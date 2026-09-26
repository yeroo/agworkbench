"""Helper workspace selection and inbox waiting; no live terminal calls or real sleeps."""

import contextlib
import io
import json
import os
import shutil
import subprocess
import stat
import sys
import tempfile
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

    def test_the_relay_reports_with_the_workbench_loop_id_not_a_runtime(self):
        # #45: the relay is not a Claude runtime; it passes the id claude.json holds, and only that id.
        with patch.dict(os.environ):
            os.environ.pop('CLAUDE_CODE_SESSION_ID')
            report = self.q.write_loop_state(self.folder, 'blocked', reason='stalled: idle', loop_id=self.loop)
            self.assertEqual(('blocked', self.loop, 1), (report['state'], report['loopId'], report['rev']))
            with self.assertRaises(self.q.QueueError):
                self.q.write_loop_state(self.folder, 'blocked', reason='x', loop_id=str(uuid.uuid4()))
            with self.assertRaises(self.q.QueueError):
                self.q.write_loop_state(self.folder, 'blocked', reason='x')      # no runtime, no id
        self.assertEqual(1, self.q.read_json(self.state / 'loop.json')['rev'])

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
            self.assertEqual('direct', args['command-mode'])     # no shell: the ended pane takes no input (#33)
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


class HelperCommand(unittest.TestCase):
    """#33: helpers run in agwinterm's direct mode, so their command line is Windows-quoted."""

    @unittest.skipUnless(sys.platform == 'win32', 'Windows quoting')
    def test_helper_command_line_survives_windows_quoting_and_runs_the_script(self):
        import ctypes
        from ctypes import wintypes
        parse = ctypes.windll.shell32.CommandLineToArgvW
        parse.argtypes, parse.restype = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)], ctypes.POINTER(wintypes.LPWSTR)
        values = {'Checkout': 'C:\\dir with space\\','Base': 'it\'s "quoted"', 'Round': '2'}
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / 'dump args.ps1'
            script.write_text('param($Checkout, $Base, $Round)\n'
                              '[Console]::Out.Write((ConvertTo-Json -Compress @($Checkout, $Base, $Round)))\n',
                              encoding='utf-8')
            command = wb.pane_command(str(script), **values)
            count = ctypes.c_int()
            argv = parse(command, ctypes.byref(count))
            parsed = [argv[i] for i in range(count.value)]
            ctypes.windll.kernel32.LocalFree(argv)
            self.assertEqual(['-NoLogo', '-ExecutionPolicy', 'Bypass', '-File', str(script),
                              '-Checkout', values['Checkout'], '-Base', values['Base'], '-Round', '2'], parsed[1:])
            out = subprocess.run(command, capture_output=True, text=True, timeout=60).stdout
            self.assertEqual([values['Checkout'], values['Base'], '2'], json.loads(out.splitlines()[-1]))  # after any profile output


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
        self.assertIn("-Profile comprehensive", self.run_round())

    def test_claude_implementer_uses_the_saved_claude_only_profile(self):
        self.save('{"tool": "claude", "revmuxProfile": "claude-only"}')
        self.assertIn("-Profile claude-only", self.run_round())

    def test_explicit_profile_wins(self):
        self.save('{"tool": "claude", "revmuxProfile": "claude-only"}')
        self.assertIn("-Profile codex-final", self.run_round('--profile', 'codex-final'))

    def test_unreadable_or_unsafe_saved_profile_falls_back(self):
        for text in ('not json', '{"revmuxProfile": "x\' ; calc"}', '[]'):
            with self.subTest(text=text):
                self.save(text)
                self.assertIn("-Profile comprehensive", self.run_round())


HEAD = 'a' * 40
MARK = wb.PLANNER_MARKER


def clean_pr(**changes):
    pr = dict(number=7, url='https://github.com/o/r/pull/7', state='OPEN', mergeable='MERGEABLE',
              mergeStateStatus='CLEAN', reviewDecision='', headRefOid=HEAD, reviews=[], comments=[])
    pr.update(changes)
    return pr


def comment(body, when, who='yeroo'):
    return {'body': body, 'createdAt': when, 'author': {'login': who}}


class MergeCheck(unittest.TestCase):
    """#23: the read-only auto-merge gate. Conditions are pure; one test covers the gh wiring."""

    def setUp(self):
        self.folder = Path(__file__).resolve().parent.parent / ('test wb merge ' + uuid.uuid4().hex)
        self.state = self.folder / '.workbench/state'
        self.state.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.folder)
        self.addCleanup(hub.reload_paths)
        self.enterContext(patch.dict(os.environ, {'AI_HUB': str(self.folder / '.workbench'), 'AI_BOX': 'claude'}))
        hub.reload_paths()
        self.seen([7])
        self.out = io.StringIO()
        self.enterContext(contextlib.redirect_stdout(self.out))

    def seen(self, numbers):
        (self.state / 'relay.json').write_text(json.dumps({'seen_open': numbers}), encoding='utf-8')

    def mail(self, sender, folder=''):
        box = self.folder / '.workbench/inbox/claude' / folder
        box.mkdir(parents=True, exist_ok=True)
        (box / f'm-{sender}.md').write_text(f'---\nid: m-{sender}\nfrom: {sender}\nto: claude\nsubject: note\n---\nbody\n',
                                            encoding='utf-8')

    def failures(self, pr=None, inline=(), head=HEAD):
        return wb.merge_failures(pr or clean_pr(), list(inline), head, self.folder)

    def run_check(self, *args, fetched=None):
        fetch = patch.object(wb, 'fetch_pr', return_value=fetched or (clean_pr(), []))
        with fetch, patch.object(sys, 'argv', ['wb.py', 'merge-check', *args]):
            return wb.main()

    # --- the verdict ------------------------------------------------------------------------------

    def test_every_condition_holding_prints_ok(self):
        self.assertEqual([], self.failures())
        self.assertEqual(0, self.run_check('--pr', '7', '--head', HEAD))
        self.assertEqual('ok', self.out.getvalue().strip())

    def test_failures_exit_1_one_prefixed_line_each(self):
        self.seen([])
        code = self.run_check('--pr', '7', '--head', HEAD,
                              fetched=(clean_pr(state='CLOSED', headRefOid='b' * 40), []))
        self.assertEqual(1, code)
        lines = self.out.getvalue().strip().splitlines()
        self.assertEqual(['state:', 'relay:', 'head:'], [line.split()[0] for line in lines])
        self.assertNotIn('ok', lines)

    def test_head_is_required_and_must_be_a_full_sha(self):
        for head in ([], ['--head', 'abc1234'], ['--head', 'g' * 40]):
            with self.subTest(head=head), contextlib.redirect_stderr(io.StringIO()):
                try:
                    code = self.run_check('--pr', '7', *head)
                except SystemExit as exit_:
                    code = exit_.code
                self.assertEqual(2, code)
        self.assertNotIn('ok', self.out.getvalue())

    # --- (a) GitHub's merge state --------------------------------------------------------------

    def test_only_open_mergeable_clean_passes(self):
        for status in ('UNSTABLE', 'BLOCKED', 'UNKNOWN', 'HAS_HOOKS'):
            with self.subTest(status=status):
                lines = self.failures(clean_pr(mergeStateStatus=status))
                self.assertEqual([f'mergeable: merge state is {status}, not CLEAN' +
                                  (' (retry in ~30s)' if status == 'UNKNOWN' else '')], lines)
        # #32: the branch cases are routed, not final.
        self.assertEqual(['behind: the branch is behind the base branch - an UPDATE round'],
                         self.failures(clean_pr(mergeStateStatus='BEHIND')))
        self.assertEqual(['conflict: GitHub says MERGEABLE, merge state DIRTY - an UPDATE round'],
                         self.failures(clean_pr(mergeStateStatus='DIRTY')))
        self.assertEqual(['conflict: GitHub says CONFLICTING, merge state CLEAN - an UPDATE round'],
                         self.failures(clean_pr(mergeable='CONFLICTING')))
        self.assertIn('mergeable: GitHub says UNKNOWN (retry in ~30s)', self.failures(clean_pr(mergeable='UNKNOWN')))
        self.assertIn('state: PR is MERGED, not OPEN', self.failures(clean_pr(state='MERGED')))

    # --- (b) reviews ------------------------------------------------------------------------------

    def test_changes_requested_blocks_until_that_reviewer_approves(self):
        request = {'state': 'CHANGES_REQUESTED', 'body': '', 'submittedAt': '2026-09-24T10:00:00Z', 'author': {'login': 'ann'}}
        approve = dict(request, state='APPROVED', submittedAt='2026-09-24T11:00:00Z')
        self.assertEqual(['review: ann requested changes (2026-09-24T10:00:00Z)'],
                         self.failures(clean_pr(reviews=[request])))
        self.assertEqual([], self.failures(clean_pr(reviews=[approve, request])))
        self.assertEqual(['review: the review decision is CHANGES_REQUESTED'],
                         self.failures(clean_pr(reviewDecision='CHANGES_REQUESTED')))

    # --- (c) holds --------------------------------------------------------------------------------

    def test_a_hold_from_the_author_login_blocks_unless_the_planner_marked_it(self):
        hold = comment('please hold, I want to look', '2026-09-24T10:00:00Z', who='yeroo')
        lines = self.failures(clean_pr(comments=[hold]))
        self.assertEqual(1, len(lines))
        self.assertTrue(lines[0].startswith('hold: yeroo at 2026-09-24T10:00:00Z: "please hold'), lines)
        marked = comment('please hold, I want to look\n' + MARK, '2026-09-24T10:00:00Z')
        self.assertEqual([], self.failures(clean_pr(comments=[marked])))

    def test_holds_do_not_expire_and_only_a_later_unmarked_lift_releases_them(self):
        hold = comment("don't merge yet", '2026-09-01T00:00:00Z')      # long before any later commit
        self.assertTrue(self.failures(clean_pr(comments=[hold])))
        lift = comment('go ahead', '2026-09-24T12:00:00Z')
        self.assertEqual([], self.failures(clean_pr(comments=[lift, hold])))
        early_lift = comment('resume', '2026-08-01T00:00:00Z')
        self.assertTrue(self.failures(clean_pr(comments=[hold, early_lift])))
        planner_lift = comment('go ahead\n' + MARK, '2026-09-24T12:00:00Z')
        self.assertTrue(self.failures(clean_pr(comments=[hold, planner_lift])))

    def test_hold_word_matching(self):
        # r16 M1: bodies are normalised (apostrophes, emphasis, whitespace, case) before matching.
        for body, holds in [('hold', True), ('Please WAIT', True), ('do not merge', True), ('dont merge', True),
                            ("don't merge", True), ('Don\u2019t merge this yet', True), ('Don\u02bct merge', True),
                            ('do not\nmerge', True), ('do not  merge', True), ('Do **not** merge', True),
                            ('do-not-merge', True), ('DO NOT MERGE', True), ('waiting on legal', True),
                            ('`wip`', True), ('unhold', False), ('household threshold', False),
                            ('wipe the cache', False), ('hold, then go ahead', True),
                            # r16 M2: negated lifts are holds
                            ("don't go ahead", True), ('do not resume', True), ("don't unhold", True)]:
            with self.subTest(body=body):
                self.assertEqual(holds, bool(self.failures(clean_pr(comments=[comment(body, '2026-09-24T10:00:00Z')]))))

    def test_only_the_hold_author_lifts_it_with_a_bare_directive(self):
        # r16 M2
        hold = comment('hold', '2026-09-24T10:00:00Z', who='yeroo')
        for lift, released in [(comment('go ahead', '2026-09-24T11:00:00Z', who='yeroo'), True),
                               (comment('@claude resume.', '2026-09-24T11:00:00Z', who='yeroo'), True),
                               (comment('Unhold please!', '2026-09-24T11:00:00Z', who='yeroo'), True),
                               (comment('go ahead', '2026-09-24T11:00:00Z', who='ann'), False),
                               (comment('I will resume reviewing tomorrow', '2026-09-24T11:00:00Z', who='yeroo'), False),
                               (comment('go ahead and rename X first', '2026-09-24T11:00:00Z', who='yeroo'), False),
                               (comment('go ahead', '2026-09-24T11:00:00Z', who='yeroo[bot]'), False)]:
            with self.subTest(lift=lift['body'], who=lift['author']['login']):
                self.assertEqual(not released, bool(self.failures(clean_pr(comments=[hold, lift]))))
        both = [hold, comment('wait', '2026-09-24T10:30:00Z', who='ann'),
                comment('go ahead', '2026-09-24T11:00:00Z', who='yeroo')]
        lines = self.failures(clean_pr(comments=both))
        self.assertEqual(1, len(lines))
        self.assertTrue(lines[0].startswith('hold: ann at'), lines)
        bot_hold = comment('hold', '2026-09-24T10:00:00Z', who='ci[bot]')
        self.assertTrue(self.failures(clean_pr(comments=[bot_hold, comment('go ahead', '2026-09-24T11:00:00Z', who='ci[bot]')])))

    def test_labels_title_and_description_can_hold(self):
        # r16 m3
        for name in ('do-not-merge', 'DO NOT MERGE', 'on hold', 'WIP'):
            with self.subTest(label=name):
                self.assertEqual([f"label: the PR is labelled '{name}'"], self.failures(clean_pr(labels=[{'name': name}])))
        self.assertEqual([], self.failures(clean_pr(labels=[{'name': 'enhancement'}])))
        self.assertTrue(self.failures(clean_pr(title='[WIP] auto-merge'))[0].startswith('hold: the PR title'))
        described = clean_pr(body='Do not merge until #24 lands', author={'login': 'yeroo'})
        self.assertTrue(self.failures(described)[0].startswith('hold: yeroo at PR description'))
        self.assertEqual([], self.failures(clean_pr(body='Do not merge until #24 lands\n' + MARK)))
        lifted = dict(described, comments=[comment('go ahead', '2026-09-24T11:00:00Z', who='yeroo')])
        self.assertEqual([], self.failures(lifted))

    def test_review_bodies_and_inline_comments_can_hold(self):
        review = {'state': 'COMMENTED', 'body': 'hold', 'submittedAt': '2026-09-24T10:00:00Z', 'author': {'login': 'yeroo'}}
        self.assertTrue(self.failures(clean_pr(reviews=[review])))
        inline = {'body': 'wait - this breaks X', 'created_at': '2026-09-24T10:00:00Z', 'user': {'login': 'yeroo'}}
        self.assertTrue(self.failures(inline=[inline]))

    # --- (d) relay, (e) mail, head ------------------------------------------------------------

    def test_unread_human_or_github_mail_blocks(self):
        self.mail('codex')
        self.assertEqual([], self.failures())
        self.mail('human', folder='read')
        self.assertEqual([], self.failures())
        for sender in ('human', 'github'):
            with self.subTest(sender=sender):
                self.mail(sender)
                self.assertTrue(any(line.startswith(f'mail: unread from {sender}') for line in self.failures()))

    def test_an_unreadable_unread_message_fails_closed(self):
        # r16 m1
        self.mail('human')
        with patch.object(hub, 'parse_message', side_effect=UnicodeDecodeError('utf-8', b'x', 0, 1, 'bad')):
            lines = self.failures()
        self.assertEqual(1, len(lines))
        self.assertTrue(lines[0].startswith('mail: cannot read unread message m-human.md'), lines)
        with patch.object(hub, 'parse_message', side_effect=FileNotFoundError('moved')):
            self.assertEqual([], self.failures())

    def test_the_relay_must_have_seen_the_pr_open(self):
        self.seen([6])
        self.assertEqual(["relay: the relay has not recorded PR #7 as seen open yet - wait for its 'PR is open' "
                          "mail, then check again"], self.failures())
        (self.state / 'relay.json').unlink()
        self.assertTrue(self.failures())

    def test_the_tested_head_must_be_the_pr_head(self):
        self.assertTrue(self.failures(head='b' * 40)[0].startswith('head: the PR head is ' + HEAD))
        self.assertEqual([], self.failures(head=HEAD.upper()))

    # --- the gh boundary ------------------------------------------------------------------------

    def test_one_view_and_the_inline_comments_are_fetched(self):
        calls = []

        def fake(argv, **kwargs):
            calls.append(argv)
            payload = clean_pr() if argv[1:3] == ['pr', 'view'] else [[{'body': 'a'}], [{'body': 'b'}]]
            return subprocess.CompletedProcess(argv, 0, json.dumps(payload), '')
        with patch.object(wb.subprocess, 'run', side_effect=fake):
            pr, inline = wb.fetch_pr('7')
        self.assertEqual(['gh', 'pr', 'view', '7', '--json', wb.PR_FIELDS], calls[0])
        self.assertEqual(['gh', 'api', 'repos/o/r/pulls/7/comments', '--paginate', '--slurp'], calls[1])
        self.assertEqual(2, len(calls))
        self.assertEqual([{'body': 'a'}, {'body': 'b'}], inline)
        for field in ('headRefOid', 'mergeStateStatus', 'reviewDecision', 'reviews', 'comments',
                      'labels', 'title', 'body', 'author'):
            self.assertIn(field, wb.PR_FIELDS)

    def test_a_gh_failure_is_not_ok(self):
        failed = subprocess.CompletedProcess([], 1, '', 'HTTP 502')
        with patch.object(wb.subprocess, 'run', return_value=failed), \
                patch.object(sys, 'argv', ['wb.py', 'merge-check', '--pr', '7', '--head', HEAD]):
            self.assertEqual(1, wb.main())
        self.assertIn('gh: ', self.out.getvalue())
        self.assertNotIn('ok\n', self.out.getvalue())


def check(name, bucket, state=None, link=None, workflow='CI'):
    return dict(name=name, bucket=bucket, state=state or {'pass': 'SUCCESS', 'fail': 'FAILURE', 'pending': 'IN_PROGRESS',
                                                            'skipping': 'SKIPPED', 'cancel': 'CANCELLED'}[bucket],
                link=link or f'https://github.com/o/r/actions/runs/9{len(name)}/job/5{len(name)}', workflow=workflow)


def remove_tree(path):
    """rmtree that also removes git's read-only object files on Windows."""
    def writable(function, target, *_):
        os.chmod(target, stat.S_IWRITE)
        function(target)
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=writable)
    else:
        shutil.rmtree(path, onerror=writable)


def scratch(case, prefix):
    """A temporary folder outside the checkout, removed after the test - and proved removed."""
    folder = Path(tempfile.mkdtemp(prefix=prefix))
    case.addCleanup(lambda: case.assertFalse(folder.exists(), f'{folder} was left behind'))
    case.addCleanup(remove_tree, folder)             # cleanups run last-in first-out: removal, then the check
    return folder


class MergeReadiness(unittest.TestCase):
    """#32: merge-check routes the ordinary cases (CI pending or failed, behind, conflict); wait-ci,
    update-check, merge-round, ci-log and ci-rerun keep the routing in code."""

    def setUp(self):
        self.folder = scratch(self, 'wb-ready-')
        self.state = self.folder / '.workbench/state'
        self.state.mkdir(parents=True)
        self.addCleanup(hub.reload_paths)
        self.enterContext(patch.dict(os.environ, {'AI_HUB': str(self.folder / '.workbench'), 'AI_BOX': 'claude'}))
        hub.reload_paths()
        (self.state / 'relay.json').write_text(json.dumps({'seen_open': [7]}), encoding='utf-8')
        self.out = io.StringIO()
        self.enterContext(contextlib.redirect_stdout(self.out))

    def classify(self, status, checks, required=()):
        return wb.check_state(clean_pr(mergeStateStatus=status), {'all': checks, 'required': set(required)})

    # --- classification -------------------------------------------------------------------------------

    def test_pending_and_failed_checks_are_routed(self):
        self.assertEqual(['ci-pending: 2 check(s) still running (build, lint) - start wb.py wait-ci'],
                         self.classify('UNSTABLE', [check('build', 'pending'), check('lint', 'pending', 'QUEUED'),
                                                    check('docs', 'pass')]))
        self.assertEqual(['ci-failed: build FAILURE https://github.com/o/r/actions/runs/95/job/55'],
                         self.classify('UNSTABLE', [check('build', 'fail'), check('docs', 'pass')]))
        self.assertEqual([], [line for line in self.classify('UNSTABLE', [check('e2e', 'skipping'), check('b', 'fail')])
                              if 'e2e' in line])                              # skipped checks never count

    def test_required_checks_decide_and_an_optional_failure_is_final(self):
        lines = self.classify('UNSTABLE', [check('build', 'pass'), check('codecov/patch', 'fail')], required=['build'])
        self.assertEqual(['ci-optional-failed: codecov/patch FAILURE (not a required check; the human decides)'], lines)
        lines = self.classify('BLOCKED', [check('build', 'pending', 'EXPECTED'), check('codecov/patch', 'pending')],
                              required=['build'])
        self.assertEqual(['ci-pending: 2 check(s) still running (build, codecov/patch) - start wb.py wait-ci'], lines)
        self.assertEqual(['ci-failed: build CANCELLED https://github.com/o/r/actions/runs/95/job/55'],
                         self.classify('BLOCKED', [check('build', 'cancel')], required=['build']))

    def test_a_running_optional_check_is_waited_for(self):
        # r22 M1: GitHub keeps UNSTABLE until optional checks finish too.
        lines = self.classify('UNSTABLE', [check('build', 'pass'), check('lint-docs', 'pending')], required=['build'])
        self.assertEqual(['ci-pending: 1 check(s) still running (lint-docs) - start wb.py wait-ci'], lines)
        state, text = wb.ci_progress({'all': [check('build', 'pass'), check('lint-docs', 'pending')], 'required': {'build'}})
        self.assertEqual(('running', '1 check(s) running: lint-docs'), (state, text))

    def test_nothing_failed_is_reported_while_anything_runs(self):
        # r22 m4: a failed job next to running ones is judged when the run is over.
        lines = self.classify('UNSTABLE', [check('build', 'fail'), check('test', 'pending'), check('cov', 'fail')],
                              required=['build', 'test'])
        self.assertEqual(['ci-pending: 1 check(s) still running (test) - start wb.py wait-ci'], lines)

    def test_blocked_or_unstable_without_ci_trouble_stays_final(self):
        for status, checks in (('BLOCKED', [check('build', 'pass')]), ('UNSTABLE', []), ('BLOCKED', [])):
            with self.subTest(status=status, checks=len(checks)):
                self.assertEqual([f'mergeable: merge state is {status}, not CLEAN'], self.classify(status, checks))

    def run_check(self, pr, checks, head=HEAD):
        fetch = self.enterContext(patch.object(wb, 'fetch_checks', return_value=checks))
        with patch.object(wb, 'fetch_pr', return_value=(pr, [])), \
                patch.object(sys, 'argv', ['wb.py', 'merge-check', '--pr', '7', '--head', head]):
            return wb.main(), fetch

    def test_merge_check_reads_ci_only_for_the_tested_head_and_when_it_matters(self):
        code, fetch = self.run_check(clean_pr(mergeStateStatus='UNSTABLE'),
                                     {'all': [check('build', 'pending')], 'required': set()})
        self.assertEqual(1, code)
        self.assertIn('ci-pending: 1 check(s)', self.out.getvalue())
        fetch.assert_called_once_with('7')
        self.out.truncate(0)
        code, fetch = self.run_check(clean_pr(mergeStateStatus='UNSTABLE', headRefOid='b' * 40),
                                     {'all': [check('build', 'pending')], 'required': set()})
        fetch.assert_not_called()                                              # another head: no CI verdict
        self.assertNotIn('ci-', self.out.getvalue())
        self.assertIn('head: the PR head is', self.out.getvalue())
        code, fetch = self.run_check(clean_pr(), {'all': [], 'required': set()})
        fetch.assert_not_called()
        self.assertEqual(0, code)

    def test_gh_pr_checks_exit_codes(self):
        body = json.dumps([check('build', 'pending')])
        calls = []

        def fake(argv, **kwargs):
            calls.append(argv)
            return results.pop(0)
        for results, expected in (([subprocess.CompletedProcess([], 8, body, '')], 1),
                                  ([subprocess.CompletedProcess([], 1, body, '')], 1),
                                  ([subprocess.CompletedProcess([], 1, '', 'no checks reported on the x branch')], 0),
                                  ([subprocess.CompletedProcess([], 1, '', "no required checks reported on the 'x' branch")], 0)):
            with self.subTest(results=results), patch.object(wb.subprocess, 'run', side_effect=fake):
                self.assertEqual(expected, len(wb.gh_checks('7', required='required' in results[0].stderr)))
        self.assertEqual(['gh', 'pr', 'checks', '7', '--json', 'name,state,bucket,link,workflow'], calls[0])
        self.assertEqual('--required', calls[-1][-1])
        with patch.object(wb.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'HTTP 502')):
            with self.assertRaises(RuntimeError):
                wb.gh_checks('7')

    # --- wait-ci --------------------------------------------------------------------------------------

    def wait_ci(self, verdicts, *extra, view=None):
        clock = [0.0]
        self.enterContext(patch.object(wb, 'now', lambda: clock[0]))
        self.enterContext(patch.object(wb, 'pause', lambda seconds: clock.__setitem__(0, clock[0] + seconds)))
        views = view or (lambda: {'state': 'OPEN', 'headRefOid': HEAD})
        self.enterContext(patch.object(wb, 'gh_json', side_effect=lambda *args: views()))

        def checks(pr):
            value = verdicts.pop(0) if len(verdicts) > 1 else verdicts[0]
            if isinstance(value, Exception):
                raise value
            return {'all': value, 'required': set()}
        self.enterContext(patch.object(wb, 'fetch_checks', side_effect=checks))
        with patch.object(sys, 'argv', ['wb.py', 'wait-ci', '--pr', '7', '--head', HEAD, *extra]):
            return wb.main(), clock[0]

    def test_wait_ci_waits_for_ci_to_start_then_to_finish(self):
        code, _ = self.wait_ci([[], [check('build', 'pending')], [check('build', 'pending')],
                                [check('build', 'pass'), check('lint', 'fail')]])
        self.assertEqual(0, code)
        lines = self.out.getvalue().strip().splitlines()
        self.assertEqual(['no check has reported for this head yet', '1 check(s) running: build',
                          'CI DONE: 1 passed, 1 failed'], lines)                    # one line per change

    def test_wait_ci_with_no_ci_at_all_ends_after_the_grace(self):
        code, elapsed = self.wait_ci([[]], '--no-ci-grace', '5')
        self.assertEqual(0, code)
        self.assertGreaterEqual(elapsed, 300)
        self.assertIn('CI DONE: no CI reported for this head in 5 minutes', self.out.getvalue())

    def test_wait_ci_never_takes_a_gh_failure_for_done(self):
        code, elapsed = self.wait_ci([RuntimeError('HTTP 502')] * 12 + [[check('build', 'pass')]], '--no-ci-grace', '5')
        self.assertEqual(0, code)
        self.assertIn('CI DONE: 1 passed, 0 failed', self.out.getvalue())
        self.assertGreater(elapsed, 300)                                       # past the grace, yet not "no CI"

    def test_wait_ci_timeout_head_change_and_usage(self):
        code, _ = self.wait_ci([[check('build', 'pending')]], '--timeout', '10')
        self.assertEqual(3, code)
        self.out.truncate(0)
        code, _ = self.wait_ci([[check('build', 'pending')]], view=lambda: {'state': 'OPEN', 'headRefOid': 'b' * 40})
        self.assertEqual(4, code)
        code, _ = self.wait_ci([[]], view=lambda: {'state': 'MERGED', 'headRefOid': HEAD})
        self.assertEqual(4, code)
        with patch.object(sys, 'argv', ['wb.py', 'wait-ci', '--pr', '7', '--head', 'abc']), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(2, wb.main())

    # --- merge-round ----------------------------------------------------------------------------------

    def merge_round(self, pr, kind):
        with patch.object(sys, 'argv', ['wb.py', 'merge-round', '--pr', str(pr), '--kind', kind]):
            return wb.main()

    def test_rounds_are_counted_per_kind_and_per_pr(self):
        self.assertEqual([0, 0, 0, 1], [self.merge_round(7, 'update') for _ in range(4)])
        self.assertEqual([0, 1], [self.merge_round(7, 'conflict') for _ in range(2)])
        self.assertEqual([0, 1], [self.merge_round(7, 'ci-rerun') for _ in range(2)])
        self.assertEqual([0, 1], [self.merge_round('https://github.com/o/r/pull/7', 'ci-fix') for _ in range(2)])
        self.assertIn('the limit of 1 round(s) for PR #7 is reached - this goes to the human', self.out.getvalue())
        self.assertEqual(0, self.merge_round(8, 'conflict'))                   # a new PR starts again
        self.assertEqual({'pr': 8, 'conflict': 1}, json.loads((self.state / 'merge-rounds.json').read_text()))

    # --- ci-log and ci-rerun --------------------------------------------------------------------------

    def gh_boundary(self, checks, rerun_fails=False, view_fails=False, requeue_after=0, checks_fail=False):
        """gh at the process boundary. After a rerun starts, its checks show as pending again after
        `requeue_after` polls (None: never). A fake clock drives ci-rerun's wait for that."""
        calls, reran, polls = [], set(), [0]
        clock = [0.0]
        self.enterContext(patch.object(wb, 'now', lambda: clock[0]))
        self.enterContext(patch.object(wb, 'pause', lambda seconds: clock.__setitem__(0, clock[0] + seconds)))

        def fake(argv, **kwargs):
            calls.append(argv)
            if argv[1:3] == ['pr', 'checks']:
                if checks_fail:
                    return subprocess.CompletedProcess(argv, 1, '', 'HTTP 502')
                current = checks
                if reran and '--required' not in argv:
                    polls[0] += 1
                    if requeue_after is not None and polls[0] > requeue_after:
                        current = [dict(c, bucket='pending', state='QUEUED')
                                   if (wb.RUN_LINK.search(c['link']) or [None, None])[1] in reran else c for c in checks]
                return subprocess.CompletedProcess(argv, 1, json.dumps([] if '--required' in argv else current), '')
            if argv[1:3] == ['run', 'view']:
                if view_fails:
                    return subprocess.CompletedProcess(argv, 1, '', 'run 11 is still in progress; logs will be available when it is complete')
                return subprocess.CompletedProcess(argv, 0, '\n'.join(f'line {i}' for i in range(400)), '')
            if argv[1:3] == ['run', 'rerun']:
                if rerun_fails:
                    return subprocess.CompletedProcess(argv, 1, '', 'HTTP 403')
                reran.add(argv[3])
                return subprocess.CompletedProcess(argv, 0, '', '')
            raise AssertionError(argv)
        self.enterContext(patch.object(wb.subprocess, 'run', side_effect=fake))
        return calls

    def test_ci_log_writes_the_tail_of_each_failed_job(self):
        calls = self.gh_boundary([check('build', 'fail', link='https://github.com/o/r/actions/runs/11/job/22'),
                                  check('ext', 'fail', 'ERROR', link='https://ci.example/b/1'), check('ok', 'pass')])
        with patch.object(sys, 'argv', ['wb.py', 'ci-log', '--pr', '7']):
            self.assertEqual(0, wb.main())
        path = self.folder / '.workbench/review/ci-r1.log'
        text = path.read_text(encoding='utf-8')
        self.assertIn(['gh', 'run', 'view', '11', '--log-failed', '--job', '22'], calls)
        self.assertIn('line 399', text)
        self.assertNotIn('line 249\n', text)                                   # the last 150 lines only
        self.assertIn('ext ERROR - external CI, no log here: https://ci.example/b/1', text)
        with patch.object(sys, 'argv', ['wb.py', 'ci-log', '--pr', '7']):
            wb.main()
        self.assertTrue((self.folder / '.workbench/review/ci-r2.log').exists())

    def test_ci_log_never_passes_a_gh_error_off_as_the_log(self):
        # r22 m3
        self.gh_boundary([check('build', 'fail', link='https://github.com/o/r/actions/runs/11/job/22')], view_fails=True)
        with patch.object(sys, 'argv', ['wb.py', 'ci-log', '--pr', '7']), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(2, wb.main())
        text = (self.folder / '.workbench/review/ci-r1.log').read_text(encoding='utf-8')
        self.assertIn('(gh run view failed: run 11 is still in progress', text)
        self.assertIn('no job log could be fetched', err.getvalue())

    def test_ci_rerun_once_then_the_fix_round(self):
        calls = self.gh_boundary([check('build', 'fail', link='https://github.com/o/r/actions/runs/11/job/22'),
                                  check('test', 'fail', link='https://github.com/o/r/actions/runs/11/job/23')],
                                 requeue_after=2)
        with patch.object(sys, 'argv', ['wb.py', 'ci-rerun', '--pr', '7']):
            self.assertEqual(0, wb.main())
            self.assertIn('rerun started: 2 check(s) pending again - start wb.py wait-ci', self.out.getvalue())
            self.assertEqual(1, wb.main())                                     # the one rerun is used
        self.assertEqual([['gh', 'run', 'rerun', '11', '--failed']], [c for c in calls if c[1:3] == ['run', 'rerun']])

    def test_ci_rerun_counts_nothing_unless_a_rerun_started(self):
        # r22 M2: exit 2 is operational (retry), 1 is only a refusal; the round is spent only on a start.
        failed = [check('build', 'fail', link='https://github.com/o/r/actions/runs/11/job/22')]
        for kwargs in ({'rerun_fails': True}, {'checks_fail': True}):
            with self.subTest(**kwargs):
                self.gh_boundary(failed, **kwargs)
                with patch.object(sys, 'argv', ['wb.py', 'ci-rerun', '--pr', '7']):
                    self.assertEqual(2, wb.main())
                self.assertFalse((self.state / 'merge-rounds.json').exists())
        self.assertIn('retry ci-rerun', self.out.getvalue())

    def test_ci_rerun_waits_until_the_old_results_are_gone(self):
        # r22 m1: wait-ci must never read the failed results the rerun is replacing.
        self.gh_boundary([check('build', 'fail', link='https://github.com/o/r/actions/runs/11/job/22')], requeue_after=None)
        with patch.object(sys, 'argv', ['wb.py', 'ci-rerun', '--pr', '7']):
            self.assertEqual(2, wb.main())
        self.assertIn('rerun started, but its checks did not show as pending within 120s', self.out.getvalue())
        self.assertEqual({'pr': 7, 'ci-rerun': 1}, json.loads((self.state / 'merge-rounds.json').read_text()))

    def test_a_used_rerun_is_refused_before_any_gh_call(self):
        (self.state / 'merge-rounds.json').write_text(json.dumps({'pr': 7, 'ci-rerun': 1}), encoding='utf-8')
        calls = self.gh_boundary([check('build', 'fail')])
        with patch.object(sys, 'argv', ['wb.py', 'ci-rerun', '--pr', '7']):
            self.assertEqual(1, wb.main())
        self.assertEqual([], calls)

    def test_external_ci_cannot_be_rerun(self):
        self.gh_boundary([check('ext', 'fail', 'ERROR', link='https://ci.example/b/1')])
        with patch.object(sys, 'argv', ['wb.py', 'ci-rerun', '--pr', '7']):
            self.assertEqual(1, wb.main())
        self.assertIn('nothing to rerun: ext - go to a FIX round', self.out.getvalue())
        self.assertFalse((self.state / 'merge-rounds.json').exists())         # no round spent


class UpdateCheck(unittest.TestCase):
    """#32: an UPDATE round is exactly one merge of the pinned base into the reviewed head (real git)."""

    def setUp(self):
        self.repo = scratch(self, 'wb-update-')     # a git repo: never inside the checkout (r22b)
        (self.repo / '.workbench').mkdir(parents=True)
        self.enterContext(patch.dict(os.environ, {'AI_HUB': str(self.repo / '.workbench')}))
        self.out = io.StringIO()
        self.enterContext(contextlib.redirect_stdout(self.out))
        self.git('init', '-q', '-b', 'main')
        (self.repo / '.gitignore').write_text('.workbench/\n')
        self.write('a.txt', 'one\ntwo\nthree\n')
        self.commit('base')
        self.git('checkout', '-q', '-b', 'issue')
        self.write('b.txt', 'feature\n')
        self.commit('feature')
        self.reviewed = self.sha('HEAD')
        self.git('checkout', '-q', 'main')

    def git(self, *args, check=True):
        return subprocess.run(['git', '-C', str(self.repo), '-c', 'user.name=t', '-c', 'user.email=t@t',
                               '-c', 'core.autocrlf=false', *args], capture_output=True, text=True, check=check)

    def write(self, name, text):
        (self.repo / name).write_text(text, encoding='utf-8', newline='\n')

    def commit(self, message):
        self.git('add', '-A')
        self.git('commit', '-q', '-m', message)

    def sha(self, ref):
        return self.git('rev-parse', ref).stdout.strip()

    def check(self, base):
        with patch.object(sys, 'argv', ['wb.py', 'update-check', '--reviewed', self.reviewed, '--base', base]):
            return wb.main()

    def main_moves(self, text='one\nTWO\nthree\n'):
        self.write('a.txt', text)
        self.commit('main moves')
        base = self.sha('HEAD')
        self.git('checkout', '-q', 'issue')
        return base

    def test_a_clean_merge_is_clean(self):
        base = self.main_moves()
        self.git('merge', '--no-ff', '-q', '-m', 'update', base)
        self.assertEqual(0, self.check(base))
        self.assertIn('update: clean', self.out.getvalue())

    def test_a_resolved_conflict_is_a_conflict(self):
        base = self.main_moves()
        self.write('a.txt', 'one\nzwei\nthree\n')                              # the issue changed the same line
        self.commit('issue edit')
        self.reviewed = self.sha('HEAD')
        self.git('merge', '--no-ff', '-q', '-m', 'update', base, check=False)
        self.write('a.txt', 'one\nzwei/TWO\nthree\n')
        self.commit('resolve')
        self.assertEqual(0, self.check(base))
        self.assertIn('update: conflict - review the resolution: git show --remerge-diff', self.out.getvalue())

    def test_an_edit_slipped_into_a_clean_merge_is_a_conflict(self):
        # r22 m5: the merge itself needs no resolution; only the extra edit makes it non-empty.
        base = self.main_moves()                                               # the reviewed head from setUp
        merged = self.git('merge', '--no-ff', '--no-commit', base, check=False)
        self.assertEqual(0, merged.returncode, merged.stdout + merged.stderr)  # a clean merge
        self.assertEqual('one\nTWO\nthree\n', (self.repo / 'a.txt').read_text(encoding='utf-8'))
        self.write('b.txt', 'feature, quietly changed\n')
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'update')
        self.assertEqual(0, self.check(base))
        self.assertIn('update: conflict', self.out.getvalue())

    def test_anything_but_one_merge_of_the_pinned_base_is_refused(self):
        base = self.main_moves()
        self.git('rebase', '-q', base)                                          # a rebase: no merge commit
        self.assertEqual(1, self.check(base))
        self.assertIn('is not a merge commit', self.out.getvalue())
        self.git('reset', '-q', '--hard', self.reviewed)
        self.git('merge', '--no-ff', '-q', '-m', 'update', base)
        self.write('c.txt', 'extra\n')
        self.commit('an extra commit')                                         # smuggled work
        self.out.truncate(0)
        self.assertEqual(1, self.check(base))
        self.assertIn('exactly one (the merge) is allowed', self.out.getvalue())
        self.git('reset', '-q', '--hard', 'HEAD~1')
        self.write('b.txt', 'uncommitted\n')
        self.out.truncate(0)
        self.assertEqual(1, self.check(base))
        self.assertIn('uncommitted changes', self.out.getvalue())
        self.git('checkout', '-q', '--', 'b.txt')
        self.out.truncate(0)
        self.assertEqual(1, self.check(self.sha('main~1')))                     # not the base the mail named
        self.assertIn('not the base', self.out.getvalue())
        self.assertEqual(0, self.check(base))


class Settings(unittest.TestCase):
    def setUp(self):
        self.folder = Path(__file__).resolve().parent.parent / ('test wb settings ' + uuid.uuid4().hex)
        (self.folder / '.workbench/state').mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.folder)
        self.config = self.folder / 'config.json'
        self.enterContext(patch.dict(os.environ, {'AI_HUB': str(self.folder / '.workbench'),
                                                 'AGWORKBENCH_CONFIG': str(self.config)}))

    def printed(self, record=None):
        if record is not None:
            (self.folder / '.workbench/state/implementer.json').write_text(record, encoding='utf-8')
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch.object(sys, 'argv', ['wb.py', 'settings']):
            self.assertEqual(0, wb.main())
        return out.getvalue().strip()

    def test_defaults_and_records(self):
        self.assertEqual('implementer=codex revmuxProfile=comprehensive autoMerge=false autonomous=false failover=true', self.printed())
        # a #20 record has no autoMerge key: off
        self.assertEqual('implementer=claude revmuxProfile=claude-only autoMerge=false autonomous=false failover=true',
                         self.printed('{"tool": "claude", "revmuxProfile": "claude-only"}'))
        self.assertEqual('implementer=claude revmuxProfile=claude-only autoMerge=true autonomous=false failover=true',
                         self.printed('{"tool": "claude", "revmuxProfile": "claude-only", "autoMerge": true}'))
        self.assertIn('autoMerge=false', self.printed('{"tool": "codex", "autoMerge": "true"}'))

    def test_failover_is_on_unless_the_config_says_false(self):
        # #24
        for text, shown in (('{"failover": false}', 'false'), ('{"failover": true}', 'true'), ('{}', 'true'),
                            ('not json', 'true'), ('{"failover": 0}', 'true')):
            with self.subTest(config=text):
                self.config.write_text(text, encoding='utf-8')
                self.assertTrue(self.printed().endswith('failover=' + shown))


class Handover(unittest.TestCase):
    """#24: the facts for a HANDOVER mail, computed from the mailbox instead of guessed."""

    def setUp(self):
        self.folder = Path(__file__).resolve().parent.parent / ('test wb handover ' + uuid.uuid4().hex)
        self.hub = self.folder / '.workbench'
        (self.hub / 'state').mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.folder)
        self.addCleanup(hub.reload_paths)
        self.enterContext(patch.dict(os.environ, {'AI_HUB': str(self.hub)}))
        hub.reload_paths()

    def mail(self, box, mid, sender, subject, folder=''):
        directory = self.hub / 'inbox' / box / folder
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f'{mid}.md').write_text(f'---\nid: {mid}\nfrom: {sender}\nto: {box}\nsubject: {subject}\n---\nbody\n',
                                             encoding='utf-8')

    def test_the_newest_unanswered_request_is_open(self):
        self.mail('codex', '20260925T080000Z-claude-0001', 'claude', 'plan v1', folder='read')
        self.mail('claude', '20260925T081000Z-codex-0002', 'codex', 're: plan v1', folder='read')
        self.mail('codex', '20260925T082000Z-claude-0003', 'claude', 'IMPLEMENT plan v2', folder='archive')
        self.mail('codex', '20260925T083000Z-human-0004', 'human', 'not from the planner')
        pending, recent = wb.open_request()
        self.assertEqual('20260925T082000Z-claude-0003', pending['id'])
        self.assertEqual(['plan v1', 'IMPLEMENT plan v2'], [m['subject'] for m in recent])

    def test_an_answered_request_is_not_open(self):
        self.mail('codex', '20260925T080000Z-claude-0001', 'claude', 'FIX r1', folder='read')
        self.mail('claude', '20260925T081000Z-codex-0002', 'codex', 'FIXED abc')
        self.assertIsNone(wb.open_request()[0])

    def test_the_command_prints_branch_request_and_status(self):
        self.mail('codex', '20260925T080000Z-claude-0001', 'claude', 'FIX r1')
        runs = [subprocess.CompletedProcess([], 0, 'issue-24-x\n', ''), subprocess.CompletedProcess([], 0, ' M lib/wb.py\n', '')]
        out = io.StringIO()
        with patch.object(wb.subprocess, 'run', side_effect=runs), contextlib.redirect_stdout(out), \
                patch.object(sys, 'argv', ['wb.py', 'handover']):
            self.assertEqual(0, wb.main())
        text = out.getvalue()
        self.assertIn('branch: issue-24-x', text)
        self.assertIn('open request: 20260925T080000Z-claude-0001 "FIX r1" (no reply yet)', text)
        self.assertIn('   M lib/wb.py', text)

    def test_a_git_failure_is_an_error_not_a_clean_tree(self):
        # r17 i1
        self.mail('codex', '20260925T080000Z-claude-0001', 'claude', 'FIX r1')
        failed = subprocess.CompletedProcess(['git', '-C', 'x', 'status', '--short'], 128, '', 'fatal: not a git repository')
        ok = subprocess.CompletedProcess(['git', '-C', 'x', 'branch', '--show-current'], 0, 'issue-24-x\n', '')
        out, err = io.StringIO(), io.StringIO()
        with patch.object(wb.subprocess, 'run', side_effect=[ok, failed]), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err), patch.object(sys, 'argv', ['wb.py', 'handover']):
            self.assertEqual(1, wb.main())
        self.assertNotIn('(clean)', out.getvalue())
        self.assertIn('git status --short failed: fatal: not a git repository', err.getvalue())


class AutoMergeProse(unittest.TestCase):
    """#23: the planner's conditions and the implementer's exclusion are in the prose."""

    def test_planner_phase_6_and_rules(self):
        text = (Path(__file__).resolve().parent.parent / 'claude/commands/start-github-issue.md').read_text(encoding='utf-8')
        phase6 = text.split('## Phase 6')[1].split('## Phase 7')[0]
        for needle in ['wb.py" settings', 'autoMerge=true', 'wb.py" merge-check --pr <N> --head <full sha>',
                       '--match-head-commit <full sha>', 'None is deferred', 'five-round cap',
                       'whole suite passed on the PR head', 'If any condition fails, do not merge',
                       'UNKNOWN', 'go ahead']:
            self.assertIn(needle, ' '.join(phase6.split()) if ' ' in needle else phase6)
        rules = text.split('## Rules')[1]
        self.assertIn('**Never merge** unless auto-merge is on for this checkout', rules)
        self.assertIn('Never approve your own PR', rules)
        self.assertIn(wb.PLANNER_MARKER, text)
        self.assertIn('ends with the planner marker line', text.split('## Phase 5')[1].split('## Phase 6')[0])

    def test_phase_6_waits_for_the_relay_and_rechecks(self):
        # r16 M3
        text = (Path(__file__).resolve().parent.parent / 'claude/commands/start-github-issue.md').read_text(encoding='utf-8')
        auto = ' '.join(text.split('### Auto-merge')[1].split('### The human')[0].split())
        for needle in ['**Wait for the relay first.**', "`PR #N is open` mail from `github`",
                       '**Retryable failures:** `relay:`', '`mail:` (read and handle the mail)', '`UNKNOWN`',
                       'Every other failure is final for this head', '**Check again after any event',
                       'new head with the whole suite re-run on it']:
            self.assertIn(needle, auto)
        self.assertLess(auto.index('Wait for the relay first'), auto.index('merge-check --pr <N> --head <full sha>'))

    def test_the_routed_failures_are_in_the_auto_merge_branch_only(self):
        # #32
        text = (Path(__file__).resolve().parent.parent / 'claude/commands/start-github-issue.md').read_text(encoding='utf-8')
        auto = ' '.join(text.split('### Auto-merge')[1].split('### The human')[0].split())
        for needle in ['Only here, in the auto-merge branch', '| `ci-pending:` |', 'wb.py" wait-ci --pr <N> --head <full sha>',
                       'was **killed** (low memory) means "rerun merge-check", never "CI done"',
                       '| `ci-failed:` | Reported only once nothing is running', 'wb.py ci-rerun --pr <N>',
                       'counted only when a rerun started', 'Exit 2 is operational: retry ci-rerun',
                       'required or optional', 'wb.py ci-log --pr <N>', '--kind ci-fix',
                       '| `behind:` / `conflict:` |', '`UPDATE <default> <base sha>`', 'git merge --no-ff <base sha>',
                       '**never rebase, never force-push**', 'wb.py" update-check --reviewed <reviewed head sha> --base <base sha>',
                       '`update: clean`', '`update: conflict`', '--kind update', '--kind conflict', 'CANNOT-RESOLVE',
                       'never `--force` or `--force-with-lease`', '`ci-optional-failed:`, `head:`, `state:`, `mergeable:`']:
            self.assertIn(needle, auto)
        self.assertNotIn('UPDATE <default>', text.split('### The human')[1])

    def test_both_implementers_know_update(self):
        for path in ('claude/commands/workbench-implementer.md', 'codex/skills/workbench-implementer/SKILL.md'):
            with self.subTest(path=path):
                text = ' '.join((Path(__file__).resolve().parent.parent / path).read_text(encoding='utf-8').split())
                for needle in ['## UPDATE - bring the branch up to date', '`git merge --no-ff <base sha>`',
                               '**Never rebase, never `git pull`, never amend, squash or force-push**',
                               'Add nothing else to the merge commit', 'Reply `UPDATED <sha>`',
                               '`git merge --abort` and reply `CANNOT-RESOLVE <why>`', 'update-check']:
                    self.assertIn(needle, text)

    def test_implementer_never_merges(self):
        text = (Path(__file__).resolve().parent.parent / 'claude/commands/workbench-implementer.md').read_text(encoding='utf-8')
        self.assertIn("is the planner's act, never yours", text)


class UsageLimitProse(unittest.TestCase):
    """#24: what the planner does on a usage-limit mail, and what a new implementer does on HANDOVER."""
    ROOT = Path(__file__).resolve().parent.parent

    def text(self, path):
        return ' '.join((self.ROOT / path).read_text(encoding='utf-8').split())

    def test_planner_section(self):
        text = self.text('claude/commands/start-github-issue.md')
        section = text.split("## Usage limits")[1].split('## Adopted session')[0]
        for needle in ['usage limit: <box> (<tool>) <kind>', 'failover=true', "the agent's own limit message",
                       'github-workbench.cmd <owner/repo#N> -Failover', 'timeout: 600000', '(exit 3, `Failover incomplete',
                       'Exit 2 means nothing was stopped', 'wb.py" handover',
                       'subject `HANDOVER`', 'uncommitted changes are the previous implementer',
                       'refused** (exit 2, ', 'status blocked --sound', '-Implementer <tool>',
                       'never answer it', 'failover=false', 'only a record', 'never types into the limited agent']:
            self.assertIn(needle, section)

    def test_both_implementers_know_handover(self):
        for path in ('claude/commands/workbench-implementer.md', 'codex/skills/workbench-implementer/SKILL.md'):
            with self.subTest(path=path):
                text = self.text(path)
                self.assertIn('## HANDOVER - you replace another implementer mid-loop', text)
                self.assertIn('Run `git status`', text)
                self.assertIn('Continue the phase the mail names', text)


class FakeGh:
    """gh at the subprocess boundary: issue view (labels), label create, issue list, issue create."""

    def __init__(self, source_labels=(), open_issues=(), label_fails=False, create_fails=()):
        self.source_labels = list(source_labels)
        self.open_issues = list(open_issues)
        self.label_fails = label_fails
        self.create_fails = set(create_fails)
        self.calls = []
        self.bodies = {}
        self.next = 100

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        args = argv[1:]
        done = lambda out='', code=0, err='': subprocess.CompletedProcess(argv, code, out, err)
        if args[:2] == ['issue', 'view']:
            return done(json.dumps({'labels': [{'name': n} for n in self.source_labels]}))
        if args[:2] == ['label', 'create']:
            return done(code=1, err='HTTP 403: Resource not accessible') if self.label_fails else done()
        if args[:2] == ['issue', 'list']:
            return done(json.dumps(self.open_issues))
        if args[:2] == ['issue', 'create']:
            title = args[args.index('--title') + 1]
            if title in self.create_fails:
                return done(code=1, err='HTTP 502')
            self.bodies[title] = Path(args[args.index('--body-file') + 1]).read_text(encoding='utf-8')
            self.next += 1
            return done(f'https://github.com/o/r/issues/{self.next}\n')
        raise AssertionError(argv)


class FollowUps(unittest.TestCase):
    """#27: follow-ups are recorded, deduped and filed before the merge; merge-check gates on them."""

    def setUp(self):
        self.folder = Path(__file__).resolve().parent.parent / ('test wb follow ' + uuid.uuid4().hex)
        self.state = self.folder / '.workbench/state'
        self.state.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.folder)
        self.addCleanup(hub.reload_paths)
        self.enterContext(patch.dict(os.environ, {'AI_HUB': str(self.folder / '.workbench'),
                                                 'AGWORKBENCH_CONFIG': str(self.folder / 'none.json')}))
        hub.reload_paths()
        self.out, self.err = io.StringIO(), io.StringIO()
        self.enterContext(contextlib.redirect_stdout(self.out))
        self.enterContext(contextlib.redirect_stderr(self.err))

    def run_wb(self, *argv, gh=None):
        with patch.object(sys, 'argv', ['wb.py', *argv]), \
                patch.object(wb.subprocess, 'run', side_effect=gh or AssertionError('gh called')):
            return wb.main()

    def add(self, key, severity='minor', disputed=False, title=None):
        body = self.folder / f'{key}.md'
        body.write_text(f'evidence for {key}: lib/x.py:12 fails', encoding='utf-8')
        argv = ['follow-up', 'add', '--key', key, '--title', title or f'Fix {key}', '--body-file', str(body),
                '--severity', severity, '--origin', 'review r2']
        self.assertEqual(0, self.run_wb(*argv + (['--disputed'] if disputed else [])))

    def items(self):
        return json.loads((self.state / 'follow-ups.json').read_text(encoding='utf-8'))

    def settings(self, autonomous):
        (self.state / 'implementer.json').write_text(json.dumps({'tool': 'claude', 'autonomous': autonomous}),
                                                    encoding='utf-8')

    def test_add_records_and_is_idempotent_on_key(self):
        self.add('r2-m1')
        self.add('r2-m1', severity='major')
        self.assertEqual([('r2-m1', 'major', 'review r2', False)],
                         [(i['key'], i['severity'], i['origin'], i['disputed']) for i in self.items()])

    def test_file_creates_issues_with_markers_label_and_writes_urls_back(self):
        self.add('r2-m1')
        self.add('plan-queue', severity='plan')
        gh = FakeGh()
        self.assertEqual(0, self.run_wb('follow-up', 'file', '--source', '27', '--pr', '30', gh=gh))
        urls = [i['url'] for i in self.items()]
        self.assertEqual(['https://github.com/o/r/issues/101', 'https://github.com/o/r/issues/102'], urls)
        body = gh.bodies['Fix r2-m1']
        for needle in ('evidence for r2-m1', 'Source: #27, PR #30', '<!-- agworkbench:follow-up source=#27 -->',
                       wb.PLANNER_MARKER):
            self.assertIn(needle, body)
        creates = [c for c in gh.calls if c[1:3] == ['issue', 'create']]
        self.assertTrue(all(c[c.index('--label') + 1] == 'follow-up' for c in creates))
        self.assertEqual(0, self.run_wb('follow-up', 'file', '--source', '27', gh=FakeGh()))   # nothing left
        self.assertIn('no unfiled follow-ups', self.out.getvalue())

    def test_an_open_issue_with_exactly_the_title_is_reused(self):
        self.add('r2-m1', title='Fix the relay')
        gh = FakeGh(open_issues=[{'title': 'Fix the relay drain', 'url': 'u-fuzzy'},
                                 {'title': 'Fix the relay', 'url': 'https://github.com/o/r/issues/9'}])
        self.assertEqual(0, self.run_wb('follow-up', 'file', '--source', '27', gh=gh))
        self.assertEqual('https://github.com/o/r/issues/9', self.items()[0]['url'])
        self.assertFalse([c for c in gh.calls if c[1:3] == ['issue', 'create']])

    def test_a_follow_up_of_a_follow_up_gets_the_nested_label(self):
        self.add('r2-m1')
        gh = FakeGh(source_labels=['follow-up'])
        self.run_wb('follow-up', 'file', '--source', '31', gh=gh)
        create = next(c for c in gh.calls if c[1:3] == ['issue', 'create'])
        self.assertEqual('follow-up-nested', create[create.index('--label') + 1])

    def test_a_label_that_cannot_be_created_files_without_it(self):
        self.add('r2-m1')
        gh = FakeGh(label_fails=True)
        self.assertEqual(0, self.run_wb('follow-up', 'file', '--source', '27', gh=gh))
        create = next(c for c in gh.calls if c[1:3] == ['issue', 'create'])
        self.assertNotIn('--label', create)
        self.assertIn('filing without it', self.out.getvalue())

    def test_a_failed_create_keeps_the_others_and_exits_1(self):
        self.add('a')
        self.add('b')
        gh = FakeGh(create_fails={'Fix a'})
        self.assertEqual(1, self.run_wb('follow-up', 'file', '--source', '27', gh=gh))
        self.assertEqual([None, 'https://github.com/o/r/issues/101'], [i.get('url') for i in self.items()])

    def test_loop_done_requires_every_follow_up_filed(self):
        self.add('a')
        self.assertEqual(1, self.run_wb('loop-state', 'done', '--pr', '30', '--sha', 'abc'))
        self.assertFalse((self.state / 'loop-done.json').exists())
        self.run_wb('follow-up', 'file', '--source', '27', gh=FakeGh())
        self.assertEqual(0, self.run_wb('loop-state', 'done', '--pr', 'https://github.com/o/r/pull/30', '--sha', 'abc'))
        record = json.loads((self.state / 'loop-done.json').read_text(encoding='utf-8'))
        self.assertEqual((30, 'abc', ['https://github.com/o/r/issues/101']), (record['pr'], record['sha'], record['followUps']))
        self.assertEqual(2, self.run_wb('loop-state', 'done'))

    def test_settings_prints_autonomous(self):
        self.settings(True)
        self.run_wb('settings')
        self.assertIn('autonomous=true', self.out.getvalue())

    # --- merge-check's autonomous conditions ------------------------------------------------------

    def test_unfiled_follow_ups_and_severe_disputes_block_only_when_autonomous(self):
        self.add('r2-m1')
        self.add('r2-M1', severity='major', disputed=True)
        self.add('r2-m2', severity='minor', disputed=True)
        self.settings(False)
        self.assertEqual([], wb.check_follow_ups(self.folder))      # #23's gate is unchanged
        self.settings(True)
        lines = wb.check_follow_ups(self.folder)
        self.assertEqual(["review: the major finding 'r2-M1' ended disputed; an autonomous merge stops here and the human decides"],
                         [line for line in lines if line.startswith('review:')])
        self.assertEqual(3, len([line for line in lines if line.startswith('follow-up:')]))
        items = self.items()
        for item in items:
            item['url'] = 'https://github.com/o/r/issues/1'
        (self.state / 'follow-ups.json').write_text(json.dumps(items), encoding='utf-8')
        self.assertEqual(['review:'], [line.split()[0] for line in wb.check_follow_ups(self.folder)])

    def test_any_deferred_major_review_finding_blocks_but_plan_items_do_not(self):
        # r18 M4/m1: only Minor/Immaterial findings (and plan items) may be deferred when autonomous.
        self.settings(True)
        items = [{'key': 'r2-M1', 'title': 'a', 'severity': 'major', 'origin': 'review r2', 'disputed': False, 'url': 'u'},
                 {'key': 'r2-B1', 'title': 'b', 'severity': 'blocker', 'origin': 'review r2', 'disputed': True, 'url': 'u'},
                 {'key': 'plan-x', 'title': 'c', 'severity': 'plan', 'origin': 'plan', 'disputed': False, 'url': 'u'},
                 {'key': 'r2-m1', 'title': 'd', 'severity': 'minor', 'origin': 'review r2', 'disputed': True, 'url': 'u'}]
        (self.state / 'follow-ups.json').write_text(json.dumps(items), encoding='utf-8')
        self.assertEqual(["review: the major finding 'r2-M1' is deferred; an autonomous merge stops here and the human decides",
                          "review: the blocker finding 'r2-B1' ended disputed; an autonomous merge stops here and the human decides"],
                         wb.check_follow_ups(self.folder))

    def test_re_adding_a_filed_key_updates_what_merge_check_gates_on(self):
        # r18 m8
        self.settings(True)
        self.add('r2-m1')
        self.run_wb('follow-up', 'file', '--source', '27', gh=FakeGh())
        self.add('r2-m1', severity='major', disputed=True)
        item = self.items()[0]
        self.assertEqual(('major', True, 'https://github.com/o/r/issues/101'), (item['severity'], item['disputed'], item['url']))
        self.assertIn('severity/origin/disputed updated', self.out.getvalue())
        self.assertTrue(any(line.startswith('review:') for line in wb.check_follow_ups(self.folder)))

    def test_titles_are_searched_as_a_quoted_phrase_and_matched_exactly(self):
        # r18 m9
        for title in ('Fix -Failover under PowerShell 5.1', 'relay: close waits', 'Follow up #12', 'The "hold" rule'):
            with self.subTest(title=title):
                (self.state / 'follow-ups.json').unlink(missing_ok=True)
                self.add('k', title=title)
                gh = FakeGh(open_issues=[{'title': title, 'url': 'https://github.com/o/r/issues/5'}])
                self.assertEqual(0, self.run_wb('follow-up', 'file', '--source', '27', gh=gh))
                search = next(c for c in gh.calls if c[1:3] == ['issue', 'list'])
                query = search[search.index('--search') + 1]
                self.assertTrue(query.startswith('"') and query.endswith('" in:title'), query)
                self.assertNotIn('"', query[1:-len('" in:title')])      # an embedded quote cannot end the phrase
                self.assertEqual('https://github.com/o/r/issues/5', self.items()[0]['url'])

    def test_a_nested_source_also_nests(self):
        # r18 m4
        self.add('r2-m1')
        gh = FakeGh(source_labels=['follow-up-nested'])
        self.run_wb('follow-up', 'file', '--source', '31', gh=gh)
        create = next(c for c in gh.calls if c[1:3] == ['issue', 'create'])
        self.assertEqual('follow-up-nested', create[create.index('--label') + 1])

    def test_merge_failures_include_the_follow_up_gate(self):
        self.settings(True)
        self.add('r2-m1')
        (self.state / 'relay.json').write_text(json.dumps({'seen_open': [7]}), encoding='utf-8')
        lines = wb.merge_failures(clean_pr(), [], HEAD, self.folder)
        self.assertEqual(["follow-up: 'r2-m1' is not filed yet - run wb.py follow-up file, then check again"], lines)


class AutonomyProse(unittest.TestCase):
    """#27: what the planner does when autonomous."""

    def test_planner_autonomy_section_and_gates(self):
        text = ' '.join((Path(__file__).resolve().parent.parent / 'claude/commands/start-github-issue.md')
                        .read_text(encoding='utf-8').split())
        section = text.split('## Full autonomy')[1].split('## When the implementer is Claude')[0]
        for needle in ['autonomous=true', 'wb.py" follow-up add --key', '--disputed', 'follow-up file --source <N> --pr <P>',
                       'filed before the merge', 'never lower it below revmux', 'A Major or blocker **never** may, disputed or not',
                       'merge comment** lists every follow-up URL', 'loop-state done --pr <P> --sha <merged sha>',
                       'never closes on a timeout', 'brakes are unchanged']:
            self.assertIn(needle, section)
        self.assertIn('deferred **with a filed follow-up issue**; a Major or blocker never may, disputed or not',
                      text.split('## Phase 6')[1])
        self.assertIn('loop-state done --pr <P> --sha <sha>` as the very last step', text.split('## Phase 7')[1])


class LoopEndProse(unittest.TestCase):
    """#27 r18b: an implementer does not answer the end of the loop, so no unread reply blocks the close."""

    def test_both_implementers_stop_without_replying(self):
        root = Path(__file__).resolve().parent.parent
        for path in ('claude/commands/workbench-implementer.md', 'codex/skills/workbench-implementer/SKILL.md'):
            with self.subTest(path=path):
                text = ' '.join((root / path).read_text(encoding='utf-8').split())
                self.assertIn('When mail says the loop is complete, or the relay reports the PR MERGED or CLOSED', text)
                self.assertIn('do not reply', text)


if __name__ == '__main__':
    unittest.main()
