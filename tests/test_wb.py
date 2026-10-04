"""Helper workspace selection and inbox waiting; no live terminal calls or real sleeps."""

import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import stat
import sys
import time
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

    def test_live_reports_disarm_only_a_no_pr_completion(self):
        path = self.state / 'loop-done.json'
        for state, pr in (('resumed', None), ('pr-open', 'https://github.com/o/r/pull/2')):
            self.q.atomic_json(path, {'noPr': True, 'pr': None, 'issue': 1, 'at': 1})
            self.assertEqual(0, self.report(state, pr=pr))
            self.assertFalse(path.exists())
            self.assertTrue((self.state / 'loop-done-refused.json').exists())
            self.q.atomic_json(path, {'pr': 2, 'sha': 'abc'})
            self.assertEqual(0, self.report(state, pr=pr))
            self.assertEqual(2, self.q.read_json(path)['pr'])
            path.unlink()

    def test_refused_no_pr_record_is_replaced_only_when_timestamp_matches(self):
        path = self.state / 'loop-done.json'
        self.q.atomic_json(path, {'noPr': True, 'pr': None, 'issue': 1, 'at': 2})
        self.assertFalse(self.q.retire_no_pr_done(path, expected_at=1, issue=1))
        self.assertEqual(2, self.q.read_json(path)['at'])
        self.assertTrue(self.q.retire_no_pr_done(path, expected_at=2, issue=1))
        self.assertFalse(path.exists())
        self.assertEqual(2, self.q.read_json(self.state / 'loop-done-refused.json')['at'])

    def test_invalid_state_url_reason_or_identity_does_not_publish(self):
        for state, pr, reason in [('bad', None, None), ('pr-open', None, None),
                                  ('pr-open', 'https://github.com/other/repo/pull/1', None),
                                  ('blocked', None, None), ('closed', None, None)]:
            self.assertEqual(2, self.report(state, pr, reason))
        with patch.dict(os.environ, CLAUDE_CODE_SESSION_ID=str(uuid.uuid4())):
            self.assertEqual(2, self.report('blocked', reason='x'))
        self.assertFalse((self.state / 'loop.json').exists())

    def test_an_environmental_block_is_recorded_and_only_for_blocked(self):
        # #61: a block the human cannot answer (a limited tool, low disk or memory) keeps its slot.
        with patch.object(sys, 'argv', ['wb.py', 'loop-state', 'blocked', '--environmental', '--reason', 'codex limited']):
            self.assertEqual(0, wb.main())
        report = self.q.read_json(self.state / 'loop.json')
        self.assertEqual(('blocked', 'environment'), (report['state'], report['cause']))
        self.assertEqual(0, self.report('blocked', reason='a question'))
        self.assertNotIn('cause', self.q.read_json(self.state / 'loop.json'))
        with patch.object(sys, 'argv', ['wb.py', 'loop-state', 'resumed', '--environmental']):
            self.assertEqual(2, wb.main())
        with self.assertRaises(self.q.QueueError):
            self.q.write_loop_state(self.folder, 'pr-open', 'https://github.com/o/r/pull/2', cause='environment')

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
        self.assertIn('AGREED: no-op', text)
        self.assertIn('loop-state done --no-pr --reason', text)
        self.assertIn('loop-state blocked --reason "no-op: <evidence>; close the issue to finish"', text)
        self.assertIn('wb.py status blocked --sound', text)
        # #61: the warning chooser fails over like the hard limit; blocks the human cannot answer keep the slot.
        limits = text.split('## Usage limits')[1].split('## Stall pointers')[0]
        self.assertIn('`limited` or `warning`', limits)
        self.assertNotIn('never answer it. Tell the human', limits)
        self.assertIn('loop-state blocked --environmental', limits)
        self.assertEqual(2, limits.count('-Queue <spec> -ClearLimit <tool>'))    # r2 m3: the queue's own record
        self.assertIn('loop-state blocked --environmental', text.split('## Queue mode')[1].split('## The channel')[0])

    def test_no_pr_requires_a_closed_issue_and_publishes_queue_completion(self):
        (self.state / 'waiting.json').write_text('{}', encoding='utf-8')
        with patch.object(wb.subprocess, 'run', return_value=type('Done', (), {'stdout': 'issue-1-fix'})()), \
                patch.object(wb, 'gh_json', side_effect=[{'state': 'OPEN', 'stateReason': None}]):
            self.assertEqual(1, wb.loop_done_no_pr(self.folder, 'duplicate'))
        self.assertFalse((self.state / 'loop-done.json').exists())
        self.assertFalse((self.state / 'loop.json').exists())
        with patch.object(wb.subprocess, 'run', return_value=type('Done', (), {'stdout': 'issue-1-fix'})()), \
                patch.object(wb, 'gh_json', side_effect=[{'state': 'CLOSED', 'stateReason': 'NOT_PLANNED'}, []]), \
                patch.object(sys, 'argv', ['wb.py', 'loop-state', 'done', '--no-pr', '--reason', 'duplicate of #2']):
            self.assertEqual(0, wb.main())
        done = self.q.read_json(self.state / 'loop-done.json')
        self.assertEqual((None, True, 1, 'NOT_PLANNED', 'duplicate of #2'),
                         (done['pr'], done['noPr'], done['issue'], done['stateReason'], done['reason']))
        self.assertEqual('closed', self.q.read_json(self.state / 'loop.json')['state'])
        self.assertFalse((self.state / 'waiting.json').exists())

    def test_done_publishes_pr_once_and_keeps_completion_on_report_failure(self):
        self.assertEqual(0, wb.loop_done(self.folder, '457', 'sha'))
        report = self.q.read_json(self.state / 'loop.json')
        self.assertEqual(('pr-open', 'https://github.com/o/r/pull/457', 1),
                         (report['state'], report['pr'], report['rev']))
        self.assertEqual(0, wb.loop_done(self.folder, '457', 'sha'))
        self.assertEqual(1, self.q.read_json(self.state / 'loop.json')['rev'])
        with patch.dict(os.environ, CLAUDE_CODE_SESSION_ID=str(uuid.uuid4())):
            self.assertEqual(0, wb.loop_done(self.folder, '458', 'sha2'))
        self.assertEqual(458, self.q.read_json(self.state / 'loop-done.json')['pr'])
        self.assertIn('queue report failed', sys.stderr.getvalue())

    def test_done_outside_queue_does_not_publish_loop_report(self):
        (self.state / 'queue-member.json').unlink()
        self.assertEqual(0, wb.loop_done(self.folder, '457', 'sha'))
        self.assertFalse((self.state / 'loop.json').exists())

    def test_done_does_not_increment_a_canonical_case_pr_report(self):
        self.q.write_loop_state(self.folder, 'pr-open', 'https://github.com/O/R/pull/457')
        self.assertEqual(0, wb.loop_done(self.folder, '457', 'sha'))
        self.assertEqual(1, self.q.read_json(self.state / 'loop.json')['rev'])

    def test_done_refuses_a_foreign_pr_url_before_writing(self):
        self.assertEqual(2, wb.loop_done(self.folder, 'https://github.com/other/repo/pull/457', 'sha'))
        self.assertFalse((self.state / 'loop.json').exists())
        self.assertFalse((self.state / 'loop-done.json').exists())

    def test_done_replaces_an_invalid_previous_pr_report(self):
        previous = self.q.write_loop_state(self.folder, 'pr-open', 'https://github.com/o/r/pull/5')
        previous['pr'] = 'garbage'
        self.q.atomic_json(self.state / 'loop.json', previous)
        self.assertEqual(0, wb.loop_done(self.folder, '457', 'sha'))
        report = self.q.read_json(self.state / 'loop.json')
        self.assertEqual((2, 'https://github.com/o/r/pull/457'), (report['rev'], report['pr']))

    def test_no_pr_refusals_leave_no_done_record(self):
        run = patch.object(wb.subprocess, 'run', return_value=type('Done', (), {'stdout': 'issue-1-fix'})())
        with run:
            self.assertEqual(2, wb.loop_done_no_pr(self.folder, None))
            self.assertEqual(2, wb.loop_done_no_pr(self.folder, 'why', '7'))
            with patch.object(wb, 'gh_json', side_effect=RuntimeError('offline')):
                self.assertEqual(2, wb.loop_done_no_pr(self.folder, 'why'))
            with patch.object(wb, 'gh_json', side_effect=[{'state': 'CLOSED'}, [{'number': 7}]]):
                self.assertEqual(1, wb.loop_done_no_pr(self.folder, 'why'))
            wb.save_follow_ups(self.folder, [{'key': 'later', 'url': None}])
            with patch.object(wb, 'gh_json', side_effect=[{'state': 'CLOSED'}, []]):
                self.assertEqual(1, wb.loop_done_no_pr(self.folder, 'why'))
            wb.save_follow_ups(self.folder, [])
            with patch.object(wb, 'gh_json', side_effect=[{'state': 'CLOSED'}, []]), \
                    patch.object(self.q, 'write_loop_state', side_effect=self.q.QueueError('bad identity')):
                self.assertEqual(2, wb.loop_done_no_pr(self.folder, 'why'))
        self.assertFalse((self.state / 'loop-done.json').exists())

    def test_busy_done_lock_reports_queue_report_was_written(self):
        original_acquire = self.q.Lock.acquire
        def acquire(lock):
            if lock.path.name == 'loop-done.lock':
                raise self.q.QueueError('lock busy')
            return original_acquire(lock)
        err = io.StringIO()
        with patch.object(wb.subprocess, 'run', return_value=type('Done', (), {'stdout': 'issue-1-fix'})()), \
                patch.object(wb, 'gh_json', side_effect=[{'state': 'CLOSED'}, []]), \
                patch.object(self.q.Lock, 'acquire', acquire), contextlib.redirect_stderr(err):
            self.assertEqual(2, wb.loop_done_no_pr(self.folder, 'duplicate'))
        self.assertEqual('closed', self.q.read_json(self.state / 'loop.json')['state'])
        self.assertFalse((self.state / 'loop-done.json').exists())
        self.assertIn('queue report `closed` written; ', err.getvalue())
        self.assertIn('rerun to record completion', err.getvalue())

    def test_no_pr_works_outside_queue_mode(self):
        (self.state / 'queue-member.json').unlink()
        with patch.object(wb.subprocess, 'run', return_value=type('Done', (), {'stdout': 'issue-1-fix'})()), \
                patch.object(wb, 'gh_json', side_effect=[{'state': 'CLOSED', 'stateReason': 'COMPLETED'}, []]):
            self.assertEqual(0, wb.loop_done_no_pr(self.folder, 'already fixed'))
        self.assertTrue((self.state / 'loop-done.json').exists())
        self.assertFalse((self.state / 'loop.json').exists())


class HelperWorkspace(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        self.enterContext(patch.object(agw, 'use_agterm', return_value=False))  # agwinterm env vars (#60)
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
            self.assertEqual('new-session', wb.open_session(name, Path('checkout'), ['app', 'arg one'], select))
            args = self.request.call_args.kwargs['args']
            self.assertEqual('session.new', self.request.call_args.args[0])
            self.assertEqual('caller-workspace', args['workspace'])
            self.assertEqual(name, args['name'])
            self.assertEqual('checkout', args['cwd'])
            self.assertEqual('app "arg one"', args['command'])
            self.assertEqual('direct', args['command-mode'])     # no shell: the ended pane takes no input (#33)
            self.assertEqual(not select, args.get('no-select', False))
        self.assertEqual('', self.stderr.getvalue())

    def test_session_id_is_used_when_pane_id_is_absent(self):
        os.environ['AGWINTERM_SESSION_ID'] = 'unsplit-session'
        wb.open_session('revmux', Path('checkout'), ['app'], False)
        self.assertEqual('caller-workspace', self.request.call_args.kwargs['args']['workspace'])
        self.assertEqual('', self.stderr.getvalue())

    def test_unknown_or_missing_pane_omits_workspace_and_warns_once(self):
        for pane in ['', 'missing-pane']:
            os.environ['AGWINTERM_PANE_ID'] = pane
            self.stderr.seek(0)
            self.stderr.truncate()
            wb.open_session('revmux', Path('checkout'), ['app'], False)
            self.assertNotIn('workspace', self.request.call_args.kwargs['args'])
            self.assertEqual(1, len(self.stderr.getvalue().splitlines()))
            self.assertIn('workspace', self.stderr.getvalue())


def parse_windows(line):
    """The argv agwinterm's direct mode makes of a session command: CommandLineToArgvW (#33)."""
    import ctypes
    from ctypes import wintypes
    parse = ctypes.windll.shell32.CommandLineToArgvW
    parse.argtypes, parse.restype = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)], ctypes.POINTER(wintypes.LPWSTR)
    count = ctypes.c_int()
    argv = parse(line, ctypes.byref(count))
    parsed = [argv[i] for i in range(count.value)]
    ctypes.windll.kernel32.LocalFree(argv)
    return parsed


def host_refusal(line):
    """agwinterm's own check of a direct-mode command (SessionCommand.cs): the reason it refuses, or None."""
    argv = parse_windows(line)
    if len(argv[0].encode('utf-8')) >= 260:
        return 'app'
    if len(argv) - 1 > 16:
        return f'{len(argv) - 1} arguments'
    if any(len(arg.encode('utf-8')) >= 2048 for arg in argv[1:]):
        return 'argument bytes'
    return None


DUMP_PARAMS = ('Checkout', 'ScopeFile', 'Round', 'Profile', 'Run', 'Attempt', 'After', 'Base', 'Extra')


def dump_script(path):
    """A stand-in helper script that prints the parameters it was given as JSON, then exits 7."""
    names = ', '.join(f'${name}' for name in DUMP_PARAMS)
    path.write_text(f'param({names})\n'
                    '[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)\n'
                    "$given = [ordered]@{}\n"
                    "foreach ($key in $PSBoundParameters.Keys) { $given[$key] = $PSBoundParameters[$key] }\n"
                    "[Console]::Out.Write(\"`n\" + (ConvertTo-Json -Compress $given))\n"
                    'exit 7\n', encoding='utf-8-sig')


def run_dumped(line):
    """Run a session command the way the host would (already split by Windows rules) and return the
    parameters the dump script printed and its exit code."""
    done = subprocess.run(parse_windows(line), capture_output=True, timeout=120)
    out = done.stdout.decode('utf-8', 'replace')
    return json.loads(out.splitlines()[-1]), done.returncode  # after any profile output


AWKWARD = {'Checkout': 'C:\\dir with space\\', 'Base': 'it\'s "quoted" \u2018typographic\u2019 \u201a\u201b',
           'Round': '2', 'Profile': 'caf\u00e9 \u00fc \u65e5\u672c', 'Run': '$env:PATH `n $(calc) @(1)',
           'Attempt': 'two\nlines', 'Extra': 'x' * 2500}


class HelperCommand(unittest.TestCase):
    """#33: helpers run in agwinterm's direct mode, so their command line is Windows-quoted. #86: their
    parameters go into a launch file, so that line stays short whatever the parameters are."""

    def test_the_command_is_short_and_the_parameters_are_in_the_launch_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            argv = wb.pane_command(root, 'revmux-r3-2', 'run-revmux.ps1', **AWKWARD)
            launcher = root / '.workbench' / 'state' / 'helpers' / 'launch-revmux-r3-2.ps1'
            self.assertEqual(['-NoLogo', '-ExecutionPolicy', 'Bypass', '-File', str(launcher)], argv[1:])
            self.assertRegex(Path(argv[0]).name.lower(), r'^(pwsh|powershell)(\.exe)?$')
            self.assertTrue(launcher.read_bytes().startswith(b'\xef\xbb\xbf'), 'BOM: Windows PowerShell 5.1 reads UTF-8')
            text = launcher.read_text(encoding='utf-8-sig')
            self.assertIn(f"& '{wb.HERE / 'run-revmux.ps1'}' -Checkout ", text)
            self.assertIn("-Base 'it''s \"quoted\" \u2018\u2018typographic\u2019\u2019 \u201a\u201a\u201b\u201b'", text)
            self.assertTrue(text.endswith('\nexit $LASTEXITCODE\n'))
            self.assertIsNone(wb.fits_host(argv))

    @unittest.skipUnless(sys.platform == 'win32', 'Windows quoting, PowerShell')
    def test_the_launcher_runs_the_script_with_every_value_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "check out \u00e9 it's"
            script = Path(tmp) / 'dump args.ps1'
            dump_script(script)
            line = subprocess.list2cmdline(wb.pane_command(root, 'test', str(script), **AWKWARD))
            self.assertIsNone(host_refusal(line))
            given, code = run_dumped(line)
            self.assertEqual(AWKWARD, given)
            self.assertEqual(7, code, "the script's own exit code is the pane's")

    def test_fits_host_names_each_limit(self):
        app = 'C:\\pwsh.exe'
        self.assertIsNone(wb.fits_host([app, *['a'] * 16, ]))
        self.assertIsNone(wb.fits_host(['x' * 259, 'y' * 2047]))
        self.assertIn('the app is 260 bytes', wb.fits_host(['x' * 260]))
        self.assertIn('the app is 260 bytes', wb.fits_host(['\u00e9' * 130]))       # UTF-8 bytes, not characters
        self.assertEqual('17 arguments after the app', wb.fits_host([app, *['a'] * 17]))
        self.assertIn('an argument is 2048 bytes', wb.fits_host([app, 'y' * 2048]))
        self.assertIn('an argument is 2049 bytes', wb.fits_host([app, 'a', '\u00e9' * 1024 + 'z']))
        self.assertEqual('no command', wb.fits_host([]))

    def test_a_command_past_the_limits_is_refused_before_session_new(self):
        with patch.object(agw, 'request') as request, patch.object(agw, 'my_pane', return_value=None):
            for argv in (['x' * 260], ['app', *['a'] * 17], ['app', 'y' * 2048]):
                with self.subTest(argv=argv[:2]), self.assertRaises(SystemExit) as refused:
                    wb.open_session('revmux', Path('checkout'), argv, False)
                self.assertTrue(str(refused.exception).startswith(
                    "wb: helper command exceeds agwinterm's session.new limits (app 259 bytes, 16 arguments, "
                    "2047 bytes each): "), refused.exception)
            request.assert_not_called()


class HelperLaunchLimits(unittest.TestCase):
    """#86: every helper session wb.py opens fits agwinterm's session.new limits, whatever its parameters."""

    def setUp(self):
        # A long checkout path with spaces, a quote and non-ASCII, still inside MAX_PATH for the scope file.
        base = Path(__file__).resolve().parent.parent / ('test wb launch ' + uuid.uuid4().hex)
        self.addCleanup(shutil.rmtree, base, True)
        self.folder = base / ("it's a checkout \u00e9\u65e5 " + 'd' * max(1, 150 - len(str(base))))
        (self.folder / '.workbench/review').mkdir(parents=True)
        self.scope = self.folder / ("scope it's \u00e9 " + 's' * max(1, 200 - len(str(self.folder)) - 20) + '.md')
        self.scope.write_text('scope', encoding='utf-8')
        self.assertGreaterEqual(len(str(self.scope)), 190)
        self.enterContext(patch.dict(os.environ, {'AI_HUB': str(self.folder / '.workbench'), 'AI_BOX': 'codex'}))
        self.enterContext(patch.object(wb, 'issue_number', return_value='86'))
        self.enterContext(patch.object(agw, 'my_pane', return_value=None))
        self.request = self.enterContext(patch.object(agw, 'request', return_value='sid'))
        self.opened = self.enterContext(patch.object(wb, 'open_session', wraps=wb.open_session))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))

    def launch(self, *argv):
        with patch.object(sys, 'argv', ['wb.py', *argv]):
            self.assertEqual(0, wb.main())
        command = self.request.call_args.kwargs['args']['command']
        argv = self.opened.call_args.args[2]
        self.assertIsNone(wb.fits_host(argv))
        self.assertLessEqual(len(argv) - 1, 16)
        if sys.platform == 'win32':
            self.assertEqual(argv, parse_windows(command))
            self.assertIsNone(host_refusal(command))
        return command

    def limited_round(self):
        review = self.folder / '.workbench/review'
        (review / 'revmux-r3.md').write_text('limited', encoding='utf-8')
        (review / 'revmux-r3.json').write_text(json.dumps({'run': 'r3', 'dir': 'x', 'scope': str(self.scope)}),
                                               encoding='utf-8')

    def test_every_helper_launch_fits(self):
        launches = {
            'revmux': ('revmux', '--round', '3', '--scope', str(self.scope)),
            'rerun with --after': ('revmux', '--round', '3', '--rerun', '--after', '30', '--profile', 'claude-only'),
            'suite': ('suite', '--label', 'abc1234', '--', 'python', *[f'word{i}' for i in range(29)], 'z' * 3000),
            'human-review': ('human-review', '--base', 'origin/main'),
        }
        for name, argv in launches.items():
            with self.subTest(name):
                if name == 'rerun with --after':
                    self.limited_round()
                self.launch(*argv)

    @unittest.skipUnless(sys.platform == 'win32', 'Windows quoting, PowerShell')
    def test_a_rerun_runs_the_script_with_exactly_its_parameters(self):
        self.limited_round()
        lib = self.folder.parent / 'lib'
        lib.mkdir()
        dump_script(lib / 'run-revmux.ps1')
        with patch.object(wb, 'HERE', lib):
            line = self.launch('revmux', '--round', '3', '--rerun', '--after', '30', '--profile', 'claude-only')
        given, _ = run_dumped(line)
        self.assertEqual({'Checkout': str(self.folder.resolve()), 'ScopeFile': str(self.scope), 'Round': '3',
                          'Profile': 'claude-only', 'Run': 'r3-1', 'Attempt': '1', 'After': '30'}, given)

    def test_the_suite_passes_its_whole_argv_through_the_launch_file(self):
        import run_helper
        command = ['python', '-m', 'unittest', *[f'w{i}' for i in range(27)], 'a "quoted" \u00e9 ' + 'z' * 3000]
        self.launch('suite', '--label', 'abc1234', '--', *command)
        argv = self.opened.call_args.args[2]
        self.assertEqual([str(wb.HERE / 'run_helper.py'), '--args-file'], argv[1:3])
        self.assertEqual(['--hub', str(self.folder.resolve() / '.workbench'), '--label', 'abc1234', '--to', 'codex', '--',
                          *command], run_helper.read_args_file(None, argv[2:]))

    def test_a_rerun_refused_for_its_size_puts_the_limited_report_back(self):
        self.limited_round()
        with patch.object(wb, 'fits_host', return_value='17 arguments after the app'), \
                patch.object(sys, 'argv', ['wb.py', 'revmux', '--round', '3', '--rerun']), \
                self.assertRaises(SystemExit) as refused:
            wb.main()
        self.assertIn("exceeds agwinterm's session.new limits", str(refused.exception))
        self.request.assert_not_called()
        self.assertEqual(['revmux-r3.json', 'revmux-r3.md'],
                         sorted(p.name for p in (self.folder / '.workbench/review').iterdir()))


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

def launched(opened):
    """The launch file the last helper session runs (#86): the script call with its parameters."""
    return Path(opened.call_args.args[2][-1]).read_text(encoding='utf-8-sig')


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
        return launched(self.opened)

    def save(self, text):
        (self.folder / '.workbench/state/implementer.json').write_text(text, encoding='utf-8')

    def test_no_saved_implementer_keeps_comprehensive(self):
        self.assertIn("-Profile 'comprehensive'", self.run_round())

    def test_claude_implementer_uses_the_saved_claude_only_profile(self):
        self.save('{"tool": "claude", "revmuxProfile": "claude-only"}')
        self.assertIn("-Profile 'claude-only'", self.run_round())

    def test_kimi_implementer_uses_claude_only_unless_the_record_says_otherwise(self):
        # #65: the launcher records claude-only for kimi; a record without a usable profile falls
        # back the same way, since Codex is not in the loop to review.
        self.save('{"tool": "kimi", "revmuxProfile": "claude-only"}')
        self.assertIn("-Profile 'claude-only'", self.run_round())
        self.save('{"tool": "kimi"}')
        self.assertIn("-Profile 'claude-only'", self.run_round())
        self.save('{"tool": "kimi", "revmuxProfile": "codex-final"}')
        self.assertIn("-Profile 'codex-final'", self.run_round())

    def test_explicit_profile_wins(self):
        self.save('{"tool": "claude", "revmuxProfile": "claude-only"}')
        self.assertIn("-Profile 'codex-final'", self.run_round('--profile', 'codex-final'))

    def test_unreadable_or_unsafe_saved_profile_falls_back(self):
        for text in ('not json', '{"revmuxProfile": "x\' ; calc"}', '[]'):
            with self.subTest(text=text):
                self.save(text)
                self.assertIn("-Profile 'comprehensive'", self.run_round())


HEAD = 'a' * 40
MARK = wb.PLANNER_MARKER


def clean_pr(**changes):
    pr = dict(number=7, url='https://github.com/o/r/pull/7', state='OPEN', mergeable='MERGEABLE',
              mergeStateStatus='CLEAN', reviewDecision='', headRefOid=HEAD, reviews=[], comments=[])
    pr.update(changes)
    return pr


def make_origin(folder, repo='o/r'):
    """folder as the workbench's checkout of repo: gh calls name it from this origin (#71)."""
    for argv in (['git', 'init', '-q', str(folder)],
                 ['git', '-C', str(folder), 'remote', 'add', 'origin', f'https://github.com/{repo}.git']):
        subprocess.run(argv, check=True, capture_output=True)


def answers_origin(fake, repo='o/r'):
    """A subprocess.run fake that also answers the workbench's origin lookup (#71)."""
    def run(argv, **kwargs):
        if argv[:1] == ['git'] and argv[-3:] == ['remote', 'get-url', 'origin']:
            return subprocess.CompletedProcess(argv, 0, f'https://github.com/{repo}.git\n', '')
        return fake(argv, **kwargs)
    return run


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
        make_origin(self.folder)
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
        with patch.object(wb.subprocess, 'run', side_effect=answers_origin(fake)):
            pr, inline = wb.fetch_pr('7')
        self.assertEqual(['gh', 'pr', 'view', '7', '--json', wb.PR_FIELDS, '--repo', 'o/r'], calls[0])
        self.assertEqual(['gh', 'api', 'repos/o/r/pulls/7/comments', '--paginate', '--slurp'], calls[1])
        self.assertEqual(2, len(calls))
        self.assertEqual([{'body': 'a'}, {'body': 'b'}], inline)
        for field in ('headRefOid', 'mergeStateStatus', 'reviewDecision', 'reviews', 'comments',
                      'labels', 'title', 'body', 'author'):
            self.assertIn(field, wb.PR_FIELDS)

    def test_a_gh_failure_is_not_ok(self):
        failed = subprocess.CompletedProcess([], 1, '', 'HTTP 502')
        with patch.object(wb.subprocess, 'run', side_effect=answers_origin(lambda argv, **kwargs: failed)), \
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
        make_origin(self.folder)
        self.addCleanup(hub.reload_paths)
        self.config = self.folder / 'config.json'                               # never the real ~/.agworkbench.json
        self.enterContext(patch.dict(os.environ, {'AI_HUB': str(self.folder / '.workbench'), 'AI_BOX': 'claude',
                                                 'AGWORKBENCH_CONFIG': str(self.config)}))
        hub.reload_paths()
        (self.state / 'relay.json').write_text(json.dumps({'seen_open': [7]}), encoding='utf-8')
        self.out = io.StringIO()
        self.enterContext(contextlib.redirect_stdout(self.out))

    def classify(self, status, checks, required=()):
        return wb.check_state(clean_pr(mergeStateStatus=status), {'all': checks, 'required': set(required)})

    # --- classification -------------------------------------------------------------------------------

    def test_pending_and_failed_checks_are_routed(self):
        self.assertEqual(["ci-pending: 2 check(s) still running (build, lint) - wait for the relay\'s ci mail (or wb.py wait-ci)"],
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
        self.assertEqual(["ci-pending: 2 check(s) still running (build, codecov/patch) - wait for the relay\'s ci mail (or wb.py wait-ci)"], lines)
        self.assertEqual(['ci-failed: build CANCELLED https://github.com/o/r/actions/runs/95/job/55'],
                         self.classify('BLOCKED', [check('build', 'cancel')], required=['build']))

    def test_a_running_optional_check_is_waited_for(self):
        # r22 M1: GitHub keeps UNSTABLE until optional checks finish too.
        lines = self.classify('UNSTABLE', [check('build', 'pass'), check('lint-docs', 'pending')], required=['build'])
        self.assertEqual(["ci-pending: 1 check(s) still running (lint-docs) - wait for the relay\'s ci mail (or wb.py wait-ci)"], lines)
        state, text = wb.ci_progress({'all': [check('build', 'pass'), check('lint-docs', 'pending')], 'required': {'build'}})
        self.assertEqual(('running', '1 check(s) running: lint-docs'), (state, text))

    def test_nothing_failed_is_reported_while_anything_runs(self):
        # r22 m4: a failed job next to running ones is judged when the run is over.
        lines = self.classify('UNSTABLE', [check('build', 'fail'), check('test', 'pending'), check('cov', 'fail')],
                              required=['build', 'test'])
        self.assertEqual(["ci-pending: 1 check(s) still running (test) - wait for the relay\'s ci mail (or wb.py wait-ci)"], lines)

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
            with self.subTest(results=results), patch.object(wb.subprocess, 'run', side_effect=answers_origin(fake)):
                self.assertEqual(expected, len(wb.gh_checks('7', required='required' in results[0].stderr)))
        self.assertEqual(['gh', 'pr', 'checks', '7', '--json', 'name,state,bucket,link,workflow', '--repo', 'o/r'],
                         calls[0])
        self.assertEqual(['--required', '--repo', 'o/r'], calls[-1][-3:])
        failed = subprocess.CompletedProcess([], 1, '', 'HTTP 502')
        with patch.object(wb.subprocess, 'run', side_effect=answers_origin(lambda argv, **kwargs: failed)):
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
        with patch.object(sys, 'argv', ['wb.py', 'merge-round', '--pr', str(pr)]
                          + (['--summary'] if kind == 'summary' else ['--kind', kind])):
            return wb.main()

    def test_rounds_are_counted_per_kind_and_per_pr(self):
        self.assertEqual([0, 0, 0, 1], [self.merge_round(7, 'update') for _ in range(4)])
        self.assertEqual([0, 0, 0, 1], [self.merge_round(7, 'conflict') for _ in range(4)])     # #90: 3, was 1
        self.assertEqual([0, 1], [self.merge_round(7, 'ci-rerun') for _ in range(2)])
        self.assertEqual([0, 1], [self.merge_round('https://github.com/o/r/pull/7', 'ci-fix') for _ in range(2)])
        self.assertIn('the limit of 1 round(s) for PR #7 is reached - this goes to the human', self.out.getvalue())
        self.assertEqual(0, self.merge_round(8, 'conflict'))                   # a new PR starts again
        self.assertEqual({'pr': 8, 'conflict': 1}, json.loads((self.state / 'merge-rounds.json').read_text()))

    def test_the_conflict_limit_is_configurable_and_validated(self):
        # #90: mergeRounds.conflict; an invalid section is exit 2 with the reason, and nothing is counted.
        self.config.write_text(json.dumps({'mergeRounds': {'conflict': 1}}), encoding='utf-8')
        self.assertEqual([0, 1], [self.merge_round(7, 'conflict') for _ in range(2)])
        self.assertIn('conflict: the limit of 1 round(s) for PR #7 is reached', self.out.getvalue())
        self.config.write_text(json.dumps({'mergeRounds': {'conflict': 0}}), encoding='utf-8')
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(2, self.merge_round(7, 'conflict'))
            self.assertEqual(2, self.merge_round(7, 'summary'))
        self.assertIn('mergeRounds.conflict must be an integer from 1 to 10', err.getvalue())
        self.assertEqual({'pr': 7, 'conflict': 1}, json.loads((self.state / 'merge-rounds.json').read_text()))
        # r1 M1: an invalid mergeRounds never blocks counting the kinds it does not configure.
        self.assertEqual([0, 0], [self.merge_round(7, 'update'), self.merge_round(7, 'ci-fix')])
        self.assertEqual({'pr': 7, 'conflict': 1, 'update': 1, 'ci-fix': 1},
                         json.loads((self.state / 'merge-rounds.json').read_text()))
        with contextlib.redirect_stderr(io.StringIO()), patch.object(sys, 'argv', ['wb.py', 'merge-round', '--pr', '7']):
            self.assertEqual(2, wb.main())                                     # neither --kind nor --summary

    def test_the_summary_line_for_the_merge_note(self):
        # #90
        self.assertEqual(0, self.merge_round(7, 'summary'))
        self.assertIn('merge rounds: none', self.out.getvalue())
        (self.state / 'merge-rounds.json').write_text(json.dumps(
            {'pr': 7, 'update': 1, 'conflict': 1, 'conflict-small': 2}), encoding='utf-8')
        self.out.truncate(0)
        self.out.seek(0)
        self.assertEqual(0, self.merge_round(7, 'summary'))
        self.assertEqual('merge rounds: 1 clean update; conflicts: 2 small (uncounted), 1 counted of 3\n',
                         self.out.getvalue())
        self.assertEqual('merge rounds: 2 clean updates; conflicts: 1 small (uncounted), 0 counted of 5; 1 CI rerun',
                         wb.merge_summary({'update': 2, 'conflict-small': 1, 'ci-rerun': 1}, {'conflict': 5}))
        self.assertEqual(0, self.merge_round(8, 'summary'))                    # another PR's record is not this one's
        self.assertTrue(self.out.getvalue().endswith('merge rounds: none\n'))

    # --- ci-log and ci-rerun --------------------------------------------------------------------------

    def gh_boundary(self, checks, rerun_fails=False, view_fails=False, requeue_after=0, checks_fail=False, repo='o/r'):
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
        self.enterContext(patch.object(wb.subprocess, 'run', side_effect=answers_origin(fake, repo)))
        return calls

    def test_ci_log_writes_the_tail_of_each_failed_job(self):
        calls = self.gh_boundary([check('build', 'fail', link='https://github.com/o/r/actions/runs/11/job/22'),
                                  check('ext', 'fail', 'ERROR', link='https://ci.example/b/1'), check('ok', 'pass')])
        with patch.object(sys, 'argv', ['wb.py', 'ci-log', '--pr', '7']):
            self.assertEqual(0, wb.main())
        path = self.folder / '.workbench/review/ci-r1.log'
        text = path.read_text(encoding='utf-8')
        self.assertIn(['gh', 'run', 'view', '11', '--log-failed', '--job', '22', '--repo', 'o/r'], calls)
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
            self.assertIn("rerun started: 2 check(s) pending again - wait for the relay\'s ci mail (or wb.py wait-ci)", self.out.getvalue())
            self.assertEqual(1, wb.main())                                     # the one rerun is used
        self.assertEqual([['gh', 'run', 'rerun', '11', '--failed', '--repo', 'o/r']], [c for c in calls if c[1:3] == ['run', 'rerun']])

    def test_an_invalid_merge_rounds_config_never_leaves_a_rerun_uncounted(self):
        # #90 r1 M1: the rerun started, so it must be counted, and the next ci-rerun refused.
        self.config.write_text(json.dumps({'mergeRounds': {'conflict': 0}}), encoding='utf-8')
        calls = self.gh_boundary([check('build', 'fail', link='https://github.com/o/r/actions/runs/11/job/22')],
                                 requeue_after=1)
        with patch.object(sys, 'argv', ['wb.py', 'ci-rerun', '--pr', '7']):
            self.assertEqual(0, wb.main())
            self.assertEqual(1, wb.main())
        self.assertEqual({'pr': 7, 'ci-rerun': 1}, json.loads((self.state / 'merge-rounds.json').read_text()))
        self.assertEqual(1, len([c for c in calls if c[1:3] == ['run', 'rerun']]))

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
        self.config = self.repo / '.workbench/config.json'                     # never the real ~/.agworkbench.json
        self.enterContext(patch.dict(os.environ, {'AI_HUB': str(self.repo / '.workbench'),
                                                 'AGWORKBENCH_CONFIG': str(self.config)}))
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
        self.assertIn('update: conflict (small: 1 conflict hunk in 1 file; not counted) - review the resolution: '
                      'git show --remerge-diff', self.out.getvalue())               # #90
        self.assertEqual({'head': self.sha('HEAD'), 'reviewed': self.reviewed, 'base': base, 'result': 'small',
                          'hunks': 1, 'files': ['a.txt'], 'reason': ''}, self.recorded())

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
        self.assertIn('update: conflict (counted: b.txt changed outside any conflict)', self.out.getvalue())

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

    # --- small and counted conflicts (#90) ------------------------------------------------------------

    def conflict(self, regions=1, extra=None, insert=None, resolve=None, name=None, blank=False, span=1):
        """main and the issue change the same `regions` lines (`span` lines each) of a new long file, 8
        lines apart, and the merge joins both sides. `extra` also edits that line of the result (a clean
        line the merge did not conflict on), `insert` adds a line before it; `resolve` replaces the
        result (a callable gets git's conflicted text), and False commits git's markers as they are;
        `blank` empties the line after the first conflict, or puts that text there. Leaves HEAD at the
        merge; returns the base."""
        name = name or f'c{uuid.uuid4().hex[:6]}.txt'
        lines = [f'line {i}' for i in range(8 * regions + 8 + span)]
        if blank is not False:
            lines[5] = '' if blank is True else blank
        at = [4 + 8 * k + j for k in range(regions) for j in range(span)]

        def text(tag):
            return ''.join(f'{line} {tag}\n' if i in at else f'{line}\n' for i, line in enumerate(lines))

        self.git('checkout', '-q', 'main')
        (self.repo / name).parent.mkdir(parents=True, exist_ok=True)
        self.write(name, text(''))
        self.commit('a long file')
        self.git('checkout', '-q', 'issue')
        self.git('merge', '--no-ff', '-q', '-m', 'take the long file', 'main')           # reviewed history
        self.write(name, text('issue'))
        self.commit('issue edit')
        self.reviewed = self.sha('HEAD')
        self.git('checkout', '-q', 'main')
        self.write(name, text('main'))
        self.commit('main moves')
        base = self.sha('HEAD')
        self.git('checkout', '-q', 'issue')
        self.assertNotEqual(0, self.git('merge', '--no-ff', '-q', '-m', 'update', base, check=False).returncode)
        result = text('issue+main').splitlines(keepends=True)
        if extra is not None:
            result[extra] = result[extra].rstrip('\n') + ' edited\n'
        if insert is not None:
            result.insert(insert, 'an added line\n')
        if callable(resolve):
            self.write(name, resolve((self.repo / name).read_text(encoding='utf-8')))
        elif resolve is not False:
            self.write(name, resolve if resolve is not None else ''.join(result))
        self.commit('resolve')
        return base

    def recorded(self):
        return json.loads((self.repo / '.workbench/state/update-check.json').read_text(encoding='utf-8'))

    def merge_round(self, kind, pr=7):
        with patch.object(sys, 'argv', ['wb.py', 'merge-round', '--pr', str(pr), '--kind', kind]):
            return wb.main()

    def rounds(self):
        record = json.loads((self.repo / '.workbench/state/merge-rounds.json').read_text(encoding='utf-8'))
        return {kind: record.get(kind, 0) for kind in ('update', 'conflict', 'conflict-small')}

    def test_up_to_three_conflict_regions_are_small(self):
        self.assertEqual(0, self.check(self.conflict(3)))
        self.assertIn('update: conflict (small: 3 conflict hunks in 1 file; not counted)', self.out.getvalue())

    def test_many_conflict_regions_are_counted(self):
        self.assertEqual(0, self.check(self.conflict(4)))                     # the default limit is 3
        self.assertIn('update: conflict (counted: 4 conflict hunks > 3) - review the resolution', self.out.getvalue())
        self.assertEqual(('counted', 4), (self.recorded()['result'], self.recorded()['hunks']))
        self.config.write_text(json.dumps({'mergeRounds': {'smallConflictHunks': 4}}), encoding='utf-8')
        self.assertEqual(0, self.check(self.sha('HEAD^2')))
        self.assertEqual('small', self.recorded()['result'])
        self.config.write_text(json.dumps({'mergeRounds': {'smallConflictHunks': 0}}), encoding='utf-8')
        self.assertEqual(0, self.check(self.sha('HEAD^2')))
        self.assertIn('counted: small conflicts are off', self.out.getvalue())
        self.config.write_text(json.dumps({'mergeRounds': {'smallConflictHunks': 'many'}}), encoding='utf-8')
        err = io.StringIO()
        self.out.truncate(0)
        self.out.seek(0)
        with contextlib.redirect_stderr(err):                                    # r3 i2: fail closed, not exit 2
            self.assertEqual(0, self.check(self.sha('HEAD^2')))
        self.assertIn('mergeRounds.smallConflictHunks must be an integer from 0 to 20 - the conflict is counted',
                      err.getvalue())
        self.assertIn('update: conflict (counted: mergeRounds is invalid (mergeRounds.smallConflictHunks must be',
                      self.out.getvalue())
        self.assertEqual('counted', self.recorded()['result'])

    def test_an_invalid_config_never_stops_a_clean_merge(self):
        # r3 i2: mergeRounds is read only for a conflict.
        self.config.write_text(json.dumps({'mergeRounds': []}), encoding='utf-8')
        base = self.main_moves()
        self.git('merge', '--no-ff', '-q', '-m', 'update', base)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(0, self.check(base))
        self.assertIn('update: clean', self.out.getvalue())
        self.assertEqual('', err.getvalue())

    def test_a_conflict_in_a_file_the_review_flagged_is_counted(self):
        base = self.conflict(name='src/flagged.txt')
        review = self.repo / '.workbench/review'
        review.mkdir()
        (review / 'revmux-r1.md').write_text(revmux_report([('Minor', 1)]).replace(
            '### finding 0\n\nevidence', '### finding 0\n\n`src/other.txt:4-5`\n\n`src/flagged.txt:4` evidence'),
            encoding='utf-8')
        self.assertEqual(0, self.check(base))
        self.assertIn('(small: 1 conflict hunk', self.out.getvalue())        # only the location line counts
        (review / 'revmux-r2.md').write_text(revmux_report([('Major', 1)]).replace(
            '### finding 0\n\nevidence', '### finding 0\n\n`flagged.txt:272-275`, `lib/x.py:3`\n\nevidence'),
            encoding='utf-8')
        self.out.truncate(0)
        self.assertEqual(0, self.check(base))
        self.assertIn('update: conflict (counted: src/flagged.txt was flagged by review)', self.out.getvalue())
        (review / 'revmux-r2.md').unlink()
        (self.repo / '.workbench/state/follow-ups.json').write_text(json.dumps(
            [{'key': 'k', 'title': 't', 'file': 'src\\flagged.txt:12'}]), encoding='utf-8')
        self.out.truncate(0)
        self.assertEqual(0, self.check(base))
        self.assertIn('update: conflict (counted: src/flagged.txt was flagged by review)', self.out.getvalue())

    def test_any_path_a_finding_names_is_flagged(self):
        # r1 M2: brackets, a dot-less name with a line, and a space are paths; a bare word is not.
        self.assertEqual('app/[id]/page.tsx', wb.location_file('app/[id]/page.tsx:12', token=True))
        self.assertEqual('Makefile', wb.location_file('Makefile:3', token=True))
        self.assertEqual('src/my file.py', wb.location_file('src/my file.py:3-9', token=True))
        self.assertEqual('lib/x.py', wb.location_file('lib\\x.py:272-275:4', token=True))
        for word in ('refuses', 'Makefile', '--force', ''):
            self.assertEqual('', wb.location_file(word, token=True), word)
        self.assertEqual('Makefile', wb.location_file('Makefile'))              # a follow-up's file: any path
        review = self.repo / '.workbench/review'
        review.mkdir()
        (review / 'revmux-r1.md').write_text(revmux_report([('Major', 1)]).replace(
            '### finding 0\n\nevidence', '### finding 0\n\n`src/my file.py:3` and `refuses`\n\nevidence'),
            encoding='utf-8')
        self.assertEqual(0, self.check(self.conflict(name='src/my file.py')))
        self.assertIn('update: conflict (counted: src/my file.py was flagged by review)', self.out.getvalue())
        (self.repo / '.workbench/state/follow-ups.json').write_text(json.dumps(
            [{'key': 'k', 'title': 't', 'file': 'app/[id]/page.tsx'}]), encoding='utf-8')
        self.out.truncate(0)
        self.assertEqual(0, self.check(self.conflict(name='app/[id]/page.tsx')))
        self.assertIn('update: conflict (counted: app/[id]/page.tsx was flagged by review)', self.out.getvalue())
        self.out.truncate(0)
        self.assertEqual(0, self.check(self.conflict(name='Makefile')))        # named by no finding: small
        self.assertIn('(small: 1 conflict hunk in 1 file', self.out.getvalue())

    def test_the_users_diff_config_cannot_make_a_conflict_small(self):
        # r2 M1: colour codes, no context, or a dropped blank context line all read a semantic edit as small.
        for key, value, kwargs in (('color.ui', 'always', {'extra': 6}),
                                   ('diff.context', '0', {'insert': 7}),
                                   ('diff.suppressBlankEmpty', 'true', {'blank': True, 'insert': 6})):
            with self.subTest(config=key):
                self.git('config', key, value)
                self.addCleanup(self.git, 'config', '--unset', key, check=False)
                self.out.truncate(0)
                self.out.seek(0)
                self.assertEqual(0, self.check(self.conflict(**kwargs)))
                self.assertIn('update: conflict (counted: the merge changes code outside the conflict regions',
                              self.out.getvalue())
                self.git('config', '--unset', key)

    def test_a_diff_that_does_not_parse_is_counted(self):
        # r2 M1: fail closed.
        for diff in ('garbage\n', '\x1b[1mdiff --git a/x b/x\x1b[m\n\x1b[1m--- a/x\x1b[m\n@@ -1 +1 @@\n-a\n+b\n'):
            with self.subTest(diff=diff):
                self.assertEqual({'result': 'counted', 'hunks': 0, 'files': [],
                                  'reason': 'the remerge-diff could not be parsed'}, wb.classify_merge(diff, set(), 3))

    def test_a_heading_underline_is_no_conflict_marker(self):
        # r2 m1: an rst underline in context beside a resolved region.
        self.assertEqual(0, self.check(self.conflict(blank='=============')))
        self.assertIn('(small: 1 conflict hunk in 1 file; not counted)', self.out.getvalue())
        for marker in ('<<<<<<< HEAD', '=======', '>>>>>>> main', '||||||| base'):
            self.assertTrue(wb.MARKER_LEFT.match(marker), marker)
        self.out.truncate(0)
        self.assertEqual(0, self.check(self.conflict(blank='=======')))       # r3 m4: a 7-letter heading's
        self.assertIn('(small: 1 conflict hunk in 1 file; not counted)', self.out.getvalue())

    def test_a_separator_left_outside_every_hunk_is_counted(self):
        # r3 m2: "keep both sides", deleting only <<<<<<< and >>>>>>>: the ======= is in no hunk.
        def keep_both(text):
            return ''.join(line for line in text.splitlines(keepends=True)
                           if not line.startswith(('<<<<<<<', '>>>>>>>')))
        base = self.conflict(resolve=keep_both, span=6)
        diff = self.git('show', '--remerge-diff', '--format=', 'HEAD').stdout
        self.assertNotIn('=======', diff)                                      # 7 lines from either marker
        self.assertEqual('small', wb.classify_merge(diff, set(), 3)['result'])  # the diff alone cannot tell
        self.assertEqual(0, self.check(base))
        self.assertRegex(self.out.getvalue(), r'update: conflict \(counted: conflict markers left in c\w+\.txt\)')

    def test_the_remerge_diff_ignores_submodule_and_signature_config(self):
        # r3 m1, m3: diff.submodule=log and log.showSignature=true reshape git show's output.
        for flag in ('--submodule=short', '--no-show-signature', '--no-color', '-U3'):
            self.assertIn(flag, wb.REMERGE_DIFF)
        self.git('config', 'diff.submodule', 'log')
        self.git('config', 'log.showSignature', 'true')
        base = self.main_moves()
        self.git('merge', '--no-ff', '-q', '-m', 'update', base)
        self.assertEqual(0, self.check(base))
        self.assertIn('update: clean', self.out.getvalue())

    def test_diff_noprefix_does_not_hide_a_flagged_file(self):
        # r1 m1: a noprefix diff has no a/ b/, so the path would never match.
        self.git('config', 'diff.noprefix', 'true')
        (self.repo / '.workbench/state').mkdir(parents=True, exist_ok=True)
        (self.repo / '.workbench/state/follow-ups.json').write_text(json.dumps(
            [{'key': 'k', 'title': 't', 'file': 'lib/x.py:4'}]), encoding='utf-8')
        self.assertEqual(0, self.check(self.conflict(name='lib/x.py')))
        self.assertIn('update: conflict (counted: lib/x.py was flagged by review)', self.out.getvalue())

    def test_an_edit_beside_a_conflict_is_counted(self):
        # The plan v1 critique: an edit two lines from a conflict lands in the same @@ hunk.
        base = self.conflict(extra=6)
        diff = self.git('show', '--remerge-diff', '--format=', 'HEAD').stdout
        self.assertEqual(1, diff.count('\n@@'))                                 # one hunk holds both
        self.assertEqual(0, self.check(base))
        self.assertRegex(self.out.getvalue(), r'update: conflict \(counted: the merge changes code outside the '
                                              r'conflict regions in c\w+\.txt\)')
        self.out.truncate(0)
        self.assertEqual(0, self.check(self.conflict(extra=15)))               # far away: its own hunk
        self.assertIn('counted: the merge changes code outside the conflict regions', self.out.getvalue())
        self.out.truncate(0)
        self.assertEqual(0, self.check(self.conflict(insert=7)))               # only a `+` line, two lines away
        self.assertIn('counted: the merge changes code outside the conflict regions', self.out.getvalue())

    def test_markers_left_in_the_result_are_counted(self):
        self.assertEqual(0, self.check(self.conflict(resolve=False)))
        self.assertRegex(self.out.getvalue(), r'counted: conflict markers left in c\w+\.txt')

    def test_a_modify_delete_conflict_is_counted(self):
        self.git('checkout', '-q', 'issue')
        (self.repo / 'a.txt').unlink()
        self.commit('the issue deletes a.txt')
        self.reviewed = self.sha('HEAD')
        self.git('checkout', '-q', 'main')
        base = self.main_moves()
        self.assertNotEqual(0, self.git('merge', '--no-ff', '-q', '-m', 'update', base, check=False).returncode)
        self.git('rm', '-q', 'a.txt')
        self.commit('keep it deleted')
        self.assertEqual(0, self.check(base))
        self.assertIn('update: conflict (counted: a.txt: modify/delete conflict)', self.out.getvalue())

    def test_a_small_conflict_after_a_counted_one_does_not_block(self):
        # AC1: a counted round first, then a small one: not counted, and the same HEAD only once.
        self.config.write_text(json.dumps({'mergeRounds': {'conflict': 1}}), encoding='utf-8')
        self.assertEqual(0, self.check(self.conflict(4)))
        self.assertEqual(0, self.merge_round('conflict'))
        self.assertIn('conflict round 1 of 1 for PR #7', self.out.getvalue())
        self.assertEqual(0, self.check(self.conflict(1)))
        self.assertIn('(small: 1 conflict hunk in 1 file; not counted)', self.out.getvalue())
        self.assertEqual(0, self.merge_round('conflict'))
        self.assertIn('conflict round (small, not counted): 1 small so far; 1 of 1 counted rounds used for PR #7',
                      self.out.getvalue())
        self.assertEqual(0, self.merge_round('conflict'))                       # the same HEAD again: a no-op
        self.assertIn('1 small so far; 1 of 1 counted rounds used for PR #7 (already counted for', self.out.getvalue())
        self.assertEqual({'update': 0, 'conflict': 1, 'conflict-small': 1}, self.rounds())
        self.assertEqual(1, self.merge_round('conflict') if self.check(self.conflict(4)) == 0 else None)
        self.assertIn('conflict: the limit of 1 round(s) for PR #7 is reached - this goes to the human',
                      self.out.getvalue())

    def test_large_conflicts_block_after_three(self):
        # AC2
        results = []
        for _ in range(4):
            self.assertEqual(0, self.check(self.conflict(4)))
            results.append(self.merge_round('conflict'))
        self.assertEqual([0, 0, 0, 1], results)
        self.assertIn('conflict round 3 of 3 for PR #7', self.out.getvalue())
        self.assertIn('conflict: the limit of 3 round(s) for PR #7 is reached - this goes to the human',
                      self.out.getvalue())
        self.assertEqual({'update': 0, 'conflict': 3, 'conflict-small': 0}, self.rounds())

    def test_smallness_needs_update_check_for_this_head(self):
        # AC5: no record, or one for another head, is counted; the planner never declares it small.
        self.conflict(1)
        self.assertEqual(0, self.merge_round('conflict'))
        self.assertIn('conflict round 1 of 3', self.out.getvalue())
        self.assertEqual(0, self.check(self.conflict(1)))
        record = self.recorded()
        (self.repo / '.workbench/state/update-check.json').write_text(
            json.dumps({**record, 'head': self.reviewed}), encoding='utf-8')
        self.assertEqual(0, self.merge_round('conflict'))
        self.assertIn('conflict round 2 of 3', self.out.getvalue())
        self.assertEqual({'update': 0, 'conflict': 2, 'conflict-small': 0}, self.rounds())

    def test_a_kind_that_contradicts_update_check_is_refused(self):
        self.assertEqual(0, self.check(self.conflict(1)))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(2, self.merge_round('update'))
        self.assertIn('update-check said conflict for', err.getvalue())
        self.assertFalse((self.repo / '.workbench/state/merge-rounds.json').exists())
        self.git('checkout', '-q', 'main')
        self.write('d.txt', 'main only\n')
        self.commit('main moves cleanly')
        base = self.sha('HEAD')
        self.git('checkout', '-q', 'issue')
        self.reviewed = self.sha('HEAD')
        self.git('merge', '--no-ff', '-q', '-m', 'update', base)
        self.assertEqual(0, self.check(base))
        self.assertEqual('clean', self.recorded()['result'])
        with contextlib.redirect_stderr(err):
            self.assertEqual(2, self.merge_round('conflict'))
        self.assertIn('update-check said clean for', err.getvalue())
        self.assertEqual(0, self.merge_round('update'))
        self.assertEqual(0, self.merge_round('update'))                         # the same HEAD: counted once
        self.assertEqual({'update': 1, 'conflict': 0, 'conflict-small': 0}, self.rounds())


class Settings(unittest.TestCase):
    def setUp(self):
        self.folder = Path(__file__).resolve().parent.parent / ('test wb settings ' + uuid.uuid4().hex)
        (self.folder / '.workbench/state').mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.folder)
        self.config = self.folder / 'config.json'
        self.enterContext(patch.dict(os.environ, {'AI_HUB': str(self.folder / '.workbench'),
                                                 'AGWORKBENCH_CONFIG': str(self.config)}))
        self.enterContext(patch.object(wb, 'issue_facts', return_value={'title': 'Plain', 'labels': []}))
        self.enterContext(patch.object(wb, 'diff_lines', return_value=(0, 'origin/main')))

    def printed(self, record=None):
        if record is not None:
            (self.folder / '.workbench/state/implementer.json').write_text(record, encoding='utf-8')
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch.object(sys, 'argv', ['wb.py', 'settings']):
            self.assertEqual(0, wb.main())
        return out.getvalue().strip()

    def test_defaults_and_records(self):
        self.assertEqual('implementer=codex revmuxProfile=comprehensive autoMerge=false autonomous=false stopWhenNoMajor=true minRounds=1 reviewCap=5 failover=true onLimit=failover', self.printed())
        # a #20 record has no autoMerge key: off
        self.assertEqual('implementer=claude revmuxProfile=claude-only autoMerge=false autonomous=false stopWhenNoMajor=true minRounds=1 reviewCap=5 failover=true onLimit=failover',
                         self.printed('{"tool": "claude", "revmuxProfile": "claude-only"}'))
        self.assertEqual('implementer=claude revmuxProfile=claude-only autoMerge=true autonomous=false stopWhenNoMajor=true minRounds=1 reviewCap=5 failover=true onLimit=failover',
                         self.printed('{"tool": "claude", "revmuxProfile": "claude-only", "autoMerge": true}'))
        self.assertIn('autoMerge=false', self.printed('{"tool": "codex", "autoMerge": "true"}'))
        self.assertEqual('implementer=kimi revmuxProfile=claude-only autoMerge=false autonomous=false stopWhenNoMajor=true minRounds=1 reviewCap=5 failover=true onLimit=failover',
                         self.printed('{"tool": "kimi", "revmuxProfile": "claude-only"}'))
        self.assertIn('implementer=codex', self.printed('{"tool": "aider"}'))
        import hub
        self.assertIn('kimi', hub.TOOLS)

    def test_on_limit_is_printed_from_the_record(self):
        # #77: a checkout launched with -WaitOnLimit waits out usage limits; anything else fails over.
        for record, shown in (('{"tool": "kimi", "onLimit": "wait"}', 'wait'),
                              ('{"tool": "kimi", "onLimit": "failover"}', 'failover'),
                              ('{"tool": "kimi"}', 'failover'), ('{"tool": "kimi", "onLimit": "WAIT"}', 'failover'),
                              ('{"tool": "kimi", "onLimit": true}', 'failover')):
            with self.subTest(record=record):
                self.assertTrue(self.printed(record).endswith(f'onLimit={shown}'))

    def test_failover_is_on_unless_the_config_says_false(self):
        # #24
        for text, shown in (('{"failover": false}', 'false'), ('{"failover": true}', 'true'), ('{}', 'true'),
                            ('not json', 'true'), ('{"failover": 0}', 'true')):
            with self.subTest(config=text):
                self.config.write_text(text, encoding='utf-8')
                self.assertTrue(self.printed().endswith(f'failover={shown} onLimit=failover'))


def revmux_report(sections=(), statuses=('ok',) * 4, extra='', no_findings=False):
    """A stub revmux --markdown report in the shape of the real ones (# title, ## sections, ### findings,
    ## Sources table last)."""
    lines = ['# Review: workbench / r1', '', 'scope: `scope.md`', '']
    if extra:
        lines += [extra, '']
    for name, count in sections:
        lines += [f'## {name}', '']
        lines += [line for i in range(count) for line in (f'### finding {i}', '', 'evidence', '')]
    if no_findings:
        lines += ['No findings.', '']
    lines += ['## Sources', '', '| agent | executor | model | effort | tokens | raised | status |',
              '| --- | --- | --- | --- | --- | --- | --- |']
    lines += [f'| agent{i} | claude | claude-opus-5-5 | high | 100 | 0 | {status} |' for i, status in enumerate(statuses)]
    return '\n'.join(lines) + '\n'


class ReviewRound(unittest.TestCase):
    """#64: a verified revmux round's decision is recorded; merge-check and the summary read it."""

    def setUp(self):
        self.folder = Path(__file__).resolve().parent.parent / ('test wb review ' + uuid.uuid4().hex)
        self.state = self.folder / '.workbench/state'
        self.reviews = self.folder / '.workbench/review'
        self.state.mkdir(parents=True)
        self.reviews.mkdir()
        self.addCleanup(shutil.rmtree, self.folder)
        self.addCleanup(hub.reload_paths)
        make_origin(self.folder)
        self.facts, self.lines = {'title': 'Plain issue', 'labels': []}, (0, 'origin/main')
        self.enterContext(patch.object(wb, 'issue_facts', side_effect=lambda root: self.facts))
        self.enterContext(patch.object(wb, 'diff_lines', side_effect=lambda root: self.lines))
        self.config = self.folder / 'agworkbench.json'
        self.enterContext(patch.dict(os.environ, {'AI_HUB': str(self.folder / '.workbench'), 'AI_BOX': 'claude',
                                                 'AGWORKBENCH_CONFIG': str(self.config)}))
        hub.reload_paths()
        (self.state / 'relay.json').write_text(json.dumps({'seen_open': [7]}), encoding='utf-8')
        self.out, self.err = io.StringIO(), io.StringIO()
        self.enterContext(contextlib.redirect_stdout(self.out))
        self.enterContext(contextlib.redirect_stderr(self.err))

    def configure(self, review):
        self.config.write_text(json.dumps({'review': review}), encoding='utf-8')

    def wb(self, *args):
        self.out.seek(0)
        self.out.truncate()
        with patch.object(sys, 'argv', ['wb.py', *args]):
            return wb.main()

    def record(self, round_, text, *extra):
        (self.reviews / f'revmux-r{round_}.md').write_text(text, encoding='utf-8')
        return self.wb('review-round', '--round', str(round_), *extra)

    def decision(self, round_, text, *extra):
        self.assertEqual(0, self.record(round_, text, *extra), self.err.getvalue())
        return self.out.getvalue().splitlines()[0]

    def entries(self):
        path = self.state / 'review-rounds.json'
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else []

    def follow_ups(self, *items):
        (self.state / 'follow-ups.json').write_text(json.dumps(list(items)), encoding='utf-8')

    def merge_check(self):
        with patch.object(wb, 'fetch_pr', return_value=(clean_pr(), [])):
            code = self.wb('merge-check', '--pr', '7', '--head', HEAD)
        return code, self.out.getvalue().strip().splitlines()

    # --- the decision ------------------------------------------------------------------------------

    def test_a_minor_only_round_after_a_major_round_stops(self):
        # AC1: round 1 had a Major, round 2 only Minor/Immaterial.
        self.assertEqual('review: continue', self.decision(1, revmux_report([('Major', 1), ('Minor', 2)])))
        self.assertEqual('review: stop', self.decision(2, revmux_report([('Minor', 2), ('Immaterial', 1)])))
        last = self.entries()[-1]
        self.assertEqual((2, 'stop', 0, 0, True, 1, False),
                         (last['round'], last['decision'], last['severe'], last['revmuxSevere'],
                          last['stopWhenNoMajor'], last['minRounds'], last['degraded']))
        self.assertEqual(2, last['counts']['minor'])
        self.assertEqual(1, last['counts']['immaterial'])

    def test_a_severe_section_continues_up_to_the_cap(self):
        # AC2
        for heading in ('Major', 'Critical', 'Blocker', 'major'):
            with self.subTest(heading=heading):
                text = revmux_report([(heading, 1), ('Minor', 3)])
                for round_ in range(1, 5):
                    self.assertEqual('review: continue', self.decision(round_, text))
                self.assertEqual('review: cap', self.decision(5, text))
        self.assertEqual('review: stop', self.decision(5, revmux_report([('Minor', 1)])))

    def test_a_round_past_the_cap_is_recorded(self):
        # r1 m2: a round the human asks for after the PR can be round 6; a Major there is still the cap.
        self.assertEqual(2, self.record(0, revmux_report([('Minor', 1)])))
        self.assertEqual([], self.entries())
        self.assertEqual('review: cap', self.decision(6, revmux_report([('Major', 1)])))
        self.assertEqual((1, ["review: round 6 had a Major at the 5-round cap - this is the human's"]), self.merge_check())
        self.assertEqual('review: stop', self.decision(6, revmux_report([('Minor', 1)])))
        self.assertEqual((0, ['ok']), self.merge_check())

    # --- the review cap (#75) -----------------------------------------------------------------------

    def majors(self, rounds):
        return [self.decision(k, revmux_report([('Major', 1)])) for k in rounds]

    def reset_cap(self):
        for name in ('review-big.json', 'review-rounds.json'):
            (self.state / name).unlink(missing_ok=True)

    def settings_line(self):
        self.wb('settings')
        return self.out.getvalue().strip()

    def test_review_decision_reads_the_cap_from_settings(self):
        settings = {'stopWhenNoMajor': True, 'minRounds': 1, 'maxRounds': 5}
        self.assertEqual('cap', wb.review_decision(5, 1, 0, False, settings))
        self.assertEqual('continue', wb.review_decision(9, 1, 0, False, {**settings, 'cap': 10}))
        self.assertEqual('cap', wb.review_decision(10, 1, 0, False, {**settings, 'cap': 10}))
        self.assertEqual('cap', wb.review_decision(10, 0, 0, True, {**settings, 'cap': 10}))
        self.assertEqual('stop', wb.review_decision(7, 0, 1, False, {**settings, 'cap': 10}))

    def test_a_batch_issue_gets_the_ten_round_cap(self):
        # #75 AC1: continue at rounds 5-9, cap at 10; a normal issue still caps at 5.
        self.facts = {'title': 'Batch: docxy tables', 'labels': []}
        self.assertEqual(['review: continue'] * 9, self.majors(range(1, 10)))
        self.assertEqual(['review: cap'], self.majors([10]))
        last = self.entries()[-1]
        self.assertEqual((10, True, 'title starts with Batch:'), (last['cap'], last['big'], last['bigReason']))
        self.assertIn('cap=10 (big: title starts with Batch:)', self.out.getvalue())
        for title in ('batch: lower case', '  BATCH: shouting'):
            with self.subTest(title=title):
                self.reset_cap()
                self.facts = {'title': title, 'labels': []}
                self.assertEqual(['review: continue'], self.majors([5]))
        self.reset_cap()
        self.facts = {'title': 'Batching is not a batch: title', 'labels': []}
        self.assertEqual(['review: cap'], self.majors([5]))
        self.assertEqual((5, False, None), (self.entries()[-1]['cap'], self.entries()[-1]['big'],
                                            self.entries()[-1]['bigReason']))
        self.assertIn('cap=5\n', self.out.getvalue() + '\n')

    def test_a_batch_or_big_label_makes_the_issue_big(self):
        # #75 AC2
        for label in ('batch', 'big', 'BIG', 'Batch'):
            with self.subTest(label=label):
                self.reset_cap()
                self.facts = {'title': 'Plain', 'labels': ['bug', label]}
                self.assertEqual(['review: continue'], self.majors([5]))
                self.assertEqual(f'label {label}', self.entries()[-1]['bigReason'])
        self.reset_cap()
        self.facts = {'title': 'Plain', 'labels': ['bigger', 'batches']}
        self.assertEqual(['review: cap'], self.majors([5]))

    def test_the_title_falls_back_to_issue_md_when_gh_cannot_answer(self):
        self.facts = None
        (self.folder / '.workbench/issue.md').write_text('# Batch: offline\n\nhttps://x\n', encoding='utf-8')
        self.assertEqual(['review: continue'], self.majors([5]))
        self.assertEqual('title starts with Batch: (from issue.md; labels unknown)', self.entries()[-1]['bigReason'])
        self.reset_cap()
        (self.folder / '.workbench/issue.md').write_text('# Plain offline\n', encoding='utf-8')
        self.assertEqual(['review: cap'], self.majors([5]))

    def test_a_growing_diff_switches_to_the_big_cap_and_stays(self):
        # #75 AC3
        self.assertEqual('stopWhenNoMajor=true minRounds=1 reviewCap=5 failover=true onLimit=failover',
                         self.settings_line().split('autonomous=false ')[1])
        self.lines = (1500, 'origin/main')                 # at the threshold is not past it
        self.assertEqual(['review: continue'] * 4, self.majors(range(1, 5)))
        self.assertFalse((self.state / 'review-big.json').exists())
        self.lines = (1623, 'origin/main')
        self.assertEqual(['review: continue'], self.majors([5]))
        self.assertEqual('diff 1623 lines > 1500 vs origin/main', self.entries()[-1]['bigReason'])
        self.lines = (1000, 'origin/main')                 # shrinks: judged big once, stays big
        self.assertEqual(['review: continue'], self.majors([6]))
        self.assertIn('reviewCap=10 (big: diff 1623 lines > 1500 vs origin/main) failover=true', self.settings_line())
        self.configure({'bigDiffLines': 200, 'maxRoundsBig': 12})
        self.assertIn('reviewCap=12 (big: diff 1623', self.settings_line())

    def test_settings_latches_a_big_issue_it_sees(self):
        self.lines = (1600, 'origin/trunk')
        self.assertIn('reviewCap=10 (big: diff 1600 lines > 1500 vs origin/trunk)', self.settings_line())
        self.lines = (0, 'origin/trunk')
        self.assertEqual(['review: continue'], self.majors([5]))

    def test_the_big_review_flag_makes_the_issue_big(self):
        (self.state / 'implementer.json').write_text(json.dumps({'tool': 'claude', 'bigReview': True}), encoding='utf-8')
        self.assertEqual(['review: continue'], self.majors([5]))
        self.assertEqual('-BigReview', self.entries()[-1]['bigReason'])
        # -NoBigReview later does not un-judge it
        (self.state / 'implementer.json').write_text(json.dumps({'tool': 'claude', 'bigReview': False}), encoding='utf-8')
        self.assertEqual(['review: continue'], self.majors([6]))
        self.reset_cap()
        self.assertEqual(['review: cap'], self.majors([5]))

    def test_a_big_loop_still_stops_at_the_first_round_without_a_major(self):
        # #75 AC5
        self.facts = {'title': 'Batch: x', 'labels': []}
        self.assertEqual(['review: continue'] * 6, self.majors(range(1, 7)))
        self.assertEqual('review: stop', self.decision(7, revmux_report([('Minor', 2)])))
        self.assertEqual('review: clean', self.decision(8, revmux_report(no_findings=True)))

    def test_merge_check_on_a_big_loop(self):
        # #75 AC6: main records cap at rounds 5-7; a big loop records continue, and a clean round 8 merges.
        self.facts = {'title': 'Batch: x', 'labels': []}
        self.assertEqual(['review: continue'] * 7, self.majors(range(1, 8)))
        self.assertEqual((1, ['review: round 7 had 1 Major finding(s); another revmux round is due']), self.merge_check())
        self.decision(8, revmux_report(no_findings=True))
        self.assertEqual((0, ['ok']), self.merge_check())
        self.assertEqual(['continue'] * 7 + ['clean'], [e['decision'] for e in self.entries()])

    def test_merge_check_refuses_a_big_loops_major_when_stop_when_no_major_is_off(self):
        # #75 AC6: main records cap at round 6 and, with stopWhenNoMajor false, accepts it.
        self.configure({'stopWhenNoMajor': False})
        self.facts = {'title': 'Plain', 'labels': ['big']}
        self.assertEqual(['review: continue'], self.majors([6]))
        self.assertEqual((1, ['review: round 6 had 1 Major finding(s); another revmux round is due']), self.merge_check())
        self.assertEqual(['review: cap'], self.majors([10]))
        self.assertEqual((0, ['ok']), self.merge_check())

    def test_the_summary_names_the_recorded_cap(self):
        # #75 AC7
        self.configure({'stopWhenNoMajor': False})
        self.facts = {'title': 'Batch: x', 'labels': []}
        self.majors(range(1, 11))
        self.assertEqual(0, self.wb('review-round', '--summary'))
        self.assertEqual('review ended at the 10-round cap (round 10)', self.out.getvalue().strip())
        # an entry recorded before #75 has no cap: it was the fixed five
        entries = self.entries()
        for entry in entries:
            entry.pop('cap')
        (self.state / 'review-rounds.json').write_text(json.dumps(entries), encoding='utf-8')
        self.wb('review-round', '--summary')
        self.assertEqual('review ended at the 5-round cap (round 10)', self.out.getvalue().strip())

    def test_the_cap_settings_are_validated(self):
        # #75 AC8
        for review in ({'maxRounds': 0}, {'maxRounds': 21}, {'maxRounds': '5'}, {'maxRounds': True},
                       {'maxRounds': 5.0}, {'maxRoundsBig': 4}, {'maxRoundsBig': 21}, {'maxRounds': 8, 'maxRoundsBig': 7},
                       {'bigDiffLines': 0}, {'bigDiffLines': 1.5}, {'bigDiffLines': None},
                       {'minRounds': 3, 'maxRounds': 2}):
            with self.subTest(review=review):
                self.configure(review)
                self.assertEqual(2, self.record(1, revmux_report([('Minor', 1)])))
                self.assertIn('review.', self.err.getvalue())
                self.assertIn('reviewCap=invalid', self.settings_line())
        self.assertEqual([], self.entries())
        self.configure({'maxRounds': 3, 'minRounds': 3})
        self.assertEqual(['review: continue', 'review: continue', 'review: cap'], self.majors(range(1, 4)))
        self.configure({'maxRounds': 12})                  # maxRoundsBig follows a maxRounds past 10
        self.assertIn('reviewCap=12 failover', self.settings_line())

    def test_stop_when_no_major_off_keeps_todays_rule(self):
        # AC3
        self.configure({'stopWhenNoMajor': False})
        minor = revmux_report([('Minor', 1)])
        for round_ in range(1, 5):
            self.assertEqual('review: continue', self.decision(round_, minor))
        self.assertEqual('review: cap', self.decision(5, minor))
        self.assertEqual('review: clean', self.decision(3, revmux_report(no_findings=True)))

    def test_min_rounds(self):
        # AC4
        minor = revmux_report([('Minor', 1)])
        self.assertEqual('review: stop', self.decision(1, minor))
        self.configure({'minRounds': 2})
        self.assertEqual('review: continue', self.decision(1, minor))
        self.assertEqual('review: stop', self.decision(2, minor))

    def test_a_degraded_round_never_stops(self):
        # AC5: `ok, nothing raised` is what revmux writes for an agent that found nothing - healthy.
        minor = [('Minor', 1)]
        self.assertEqual('review: stop', self.decision(2, revmux_report(minor, ('ok', 'ok, nothing raised', 'OK'))))
        for statuses, extra in [(('ok', 'DEGRADED, reported nothing'), ''), (('ok', 'rate limited: quota'), ''),
                                (('failed: exit 1',), ''), (('ok', 'ok'), 'This run is DEGRADED: 1 of 4 sources ran'),
                                (('okay',), '')]:
            with self.subTest(statuses=statuses, extra=extra):
                self.assertEqual('review: continue (degraded)', self.decision(2, revmux_report(minor, statuses, extra)))
                self.assertTrue(self.entries()[-1]['degraded'])
        self.assertEqual('review: continue (degraded)',
                         self.decision(2, revmux_report(statuses=('DEGRADED, reported nothing',), no_findings=True)))
        self.assertEqual('review: cap (degraded)', self.decision(5, revmux_report(minor, ('rate limited: x',))))

    def test_a_finding_that_quotes_the_degraded_line_is_not_a_degraded_run(self):
        # r2 m1: round 2's own report quoted the phrase in a finding title and body, every row ok.
        quoting = revmux_report([('Minor', 1)]).replace(
            '### finding 0\n\nevidence\n',
            '### The degraded check matches `This run is DEGRADED` anywhere in the report text\n\n'
            '`degraded = "This run is DEGRADED" in text` searches the whole report.\n'
            'This run is DEGRADED: quoted at the start of a body line, still inside the finding.\n')
        self.assertIn('This run is DEGRADED: quoted', quoting)
        self.assertEqual('review: stop', self.decision(2, quoting))
        # revmux's own line, before the findings or dressed as a quote/emphasis, still counts.
        for extra in ('This run is DEGRADED: 3 of 4 sources ran', '> This run is DEGRADED: x', '**This run is DEGRADED: x**'):
            with self.subTest(extra=extra):
                self.assertEqual('review: continue (degraded)', self.decision(2, revmux_report([('Minor', 1)], extra=extra)))

    def test_an_incomplete_report_is_refused_and_nothing_is_recorded(self):
        # AC6: revmux writes the Sources table last, so a crashed run has none.
        header_only = '# Review: workbench / r1\n\nscope: `x`\n'
        no_rows = '# Review\n\n## Minor\n\n### a\n\n## Sources\n\n| agent | status |\n| --- | --- |\n'
        no_sections = '# Review\n\n## Sources\n\n| agent | status |\n| --- | --- |\n| a | ok |\n'
        for text in ('', header_only, no_rows, revmux_report([('Minor', 1)]).split('## Sources')[0], no_sections):
            with self.subTest(text=text[:40]):
                self.assertEqual(2, self.record(1, text))
                self.assertIn('report incomplete', self.err.getvalue())
        self.assertEqual(2, self.wb('review-round', '--round', '3'))           # no report file
        self.assertIn('cannot read', self.err.getvalue())
        self.assertEqual([], self.entries())

    def test_what_counts(self):
        self.assertEqual('review: clean', self.decision(1, revmux_report(no_findings=True)))
        # Pre-existing and Open questions are counted apart and decide nothing.
        self.assertEqual('review: clean', self.decision(1, revmux_report([('Pre-existing', 2), ('Open questions', 1)])))
        self.assertEqual({'pre-existing': 2, 'open questions': 1},
                         {k: v for k, v in self.entries()[-1]['counts'].items() if v})
        # Unknown sections are ignored; ### lines count only inside their own section.
        self.assertEqual('review: stop', self.decision(1, revmux_report([('Minor', 1), ('Notes', 3)])))
        self.assertEqual(1, self.entries()[-1]['counts']['minor'])

    def test_the_verified_severe_count(self):
        # AC9: never below revmux's without saying why.
        major = revmux_report([('Major', 1), ('Minor', 1)])
        self.assertEqual(2, self.record(2, major, '--severe', '0'))
        self.assertIn("below revmux's 1; say why with --reason", self.err.getvalue())
        self.assertEqual(2, self.record(2, major, '--severe', '0', '--reason', '  '))
        self.assertEqual(2, self.record(2, major, '--severe', '-1', '--reason', 'x'))
        self.assertEqual(2, self.record(2, major, '--reason', 'no --severe'))
        self.assertEqual([], self.entries())
        self.assertEqual('review: stop', self.decision(2, major, '--severe', '0', '--reason', 'not reproducible'))
        self.assertEqual((1, 0, 'not reproducible'),
                         (self.entries()[-1]['revmuxSevere'], self.entries()[-1]['severe'], self.entries()[-1]['reason']))
        # r1 M1: a Major verified lower is still a finding - never `clean`, and the stop rules apply.
        only_major = revmux_report([('Major', 1)])
        self.assertEqual('review: stop', self.decision(2, only_major, '--severe', '0', '--reason', 'really a Minor'))
        self.configure({'minRounds': 3})
        self.assertEqual('review: continue', self.decision(2, only_major, '--severe', '0', '--reason', 'really a Minor'))
        self.configure({'stopWhenNoMajor': False})
        self.assertEqual('review: continue', self.decision(2, only_major, '--severe', '0', '--reason', 'really a Minor'))
        self.configure({})
        # Raising it needs no reason.
        self.assertEqual('review: continue', self.decision(2, revmux_report([('Minor', 1)]), '--severe', '1'))

    def test_invalid_config_is_refused_and_nothing_is_recorded(self):
        for review in ({'stopWhenNoMajor': 'false'}, {'stopWhenNoMajor': 0}, {'minRounds': 0}, {'minRounds': 6},
                       {'minRounds': '2'}, {'minRounds': True}, {'minRounds': 1.0}, []):
            with self.subTest(review=review):
                self.configure(review)
                self.assertEqual(2, self.record(1, revmux_report([('Minor', 1)])))
                self.assertIn('review', self.err.getvalue())
        self.config.write_text('not json', encoding='utf-8')
        self.assertEqual(2, self.record(1, revmux_report([('Minor', 1)])))
        self.assertEqual([], self.entries())

    def test_settings_prints_the_review_keys(self):
        self.configure({'stopWhenNoMajor': False, 'minRounds': 3})
        self.wb('settings')
        self.assertIn('stopWhenNoMajor=false minRounds=3 reviewCap=5 failover=true', self.out.getvalue())
        self.configure({'minRounds': 9})
        self.wb('settings')
        self.assertIn('stopWhenNoMajor=invalid minRounds=invalid reviewCap=invalid', self.out.getvalue())

    def test_a_rerecorded_earlier_round_does_not_become_the_last(self):
        self.decision(2, revmux_report([('Major', 1)]))
        self.decision(3, revmux_report([('Minor', 1)]))
        self.decision(2, revmux_report([('Major', 1)]), '--severe', '2')
        self.assertEqual([2, 3], [entry['round'] for entry in self.entries()])
        self.assertEqual(2, self.entries()[0]['severe'])
        self.assertEqual(3, wb.last_review_round(self.folder)['round'])
        self.assertEqual(0, self.merge_check()[0])

    # --- the summary -------------------------------------------------------------------------------

    def test_summary_forms(self):
        # AC7
        self.assertEqual(2, self.wb('review-round', '--summary'))
        self.assertIn('no review round recorded', self.err.getvalue())
        self.decision(1, revmux_report([('Major', 1)]))
        self.assertEqual(1, self.wb('review-round', '--summary'))
        self.assertEqual('review: round 1 decided continue; no summary yet', self.out.getvalue().strip())
        self.decision(2, revmux_report([('Minor', 3)]))
        self.assertEqual(0, self.wb('review-round', '--summary'))
        self.assertEqual('review stopped: round 2 had no Major; all findings fixed', self.out.getvalue().strip())
        leftovers = 'https://github.com/o/r/issues/90'
        self.follow_ups({'key': 'r2-m1', 'severity': 'minor', 'origin': 'review r2'},
                        {'key': 'r2-m2', 'severity': 'minor', 'origin': 'Review R2', 'url': leftovers},
                        {'key': 'r1-m1', 'severity': 'minor', 'origin': 'review r1'},
                        {'key': 'plan-x', 'severity': 'plan', 'origin': 'plan'})
        self.wb('review-round', '--summary')
        self.assertEqual('review stopped: round 2 had no Major; 2 minor finding(s) deferred as follow-ups (not filed yet)',
                         self.out.getvalue().strip())
        self.follow_ups({'key': 'r2-m1', 'severity': 'minor', 'origin': 'review r2', 'url': leftovers},
                        {'key': 'r2-m2', 'severity': 'minor', 'origin': 'review r2', 'url': leftovers})
        self.wb('review-round', '--summary')
        self.assertEqual(f'review stopped: round 2 had no Major; 2 minor finding(s) in {leftovers}', self.out.getvalue().strip())
        self.decision(3, revmux_report([('Major', 1)]), '--severe', '0', '--reason', 'verified as Minor')
        self.wb('review-round', '--summary')
        self.assertEqual(f'review stopped: round 3 had no Major; 2 minor finding(s) from round 2 in {leftovers} '
                         '(revmux 1 Major+, verified 0: verified as Minor)', self.out.getvalue().strip())

    def test_summary_keeps_an_earlier_stops_minors_and_covers_the_cap(self):
        # r1 m1 and m3
        leftovers = 'https://github.com/o/r/issues/90'
        self.decision(2, revmux_report([('Minor', 2)]))
        self.decision(3, revmux_report(no_findings=True))
        self.follow_ups({'key': 'r2-m1', 'severity': 'minor', 'origin': 'review r2', 'url': leftovers})
        self.assertEqual(0, self.wb('review-round', '--summary'))
        self.assertEqual(f'review clean after round 3; 1 minor finding(s) from round 2 in {leftovers}',
                         self.out.getvalue().strip())
        self.decision(5, revmux_report([('Major', 1)]))
        self.assertEqual(1, self.wb('review-round', '--summary'))
        self.assertEqual('review: round 5 decided cap; no summary yet', self.out.getvalue().strip())
        self.configure({'stopWhenNoMajor': False})
        self.decision(5, revmux_report([('Major', 1)]))
        self.assertEqual(0, self.wb('review-round', '--summary'))
        self.assertEqual(f'review ended at the 5-round cap (round 5); 1 minor finding(s) from round 2 in {leftovers}',
                         self.out.getvalue().strip())

    # --- merge-check -------------------------------------------------------------------------------

    def test_merge_check_accepts_a_stopped_review_once_its_minors_are_filed(self):
        # AC1, not autonomous: the stop's deferred minors must be filed like autonomy's.
        self.decision(1, revmux_report([('Major', 1)]))
        self.assertEqual((1, ['review: round 1 had 1 Major finding(s); another revmux round is due']), self.merge_check())
        self.decision(2, revmux_report([('Minor', 2)]))
        self.follow_ups({'key': 'r2-m1', 'severity': 'minor', 'origin': 'review r2'})
        self.assertEqual((1, ["follow-up: 'r2-m1' is not filed yet - run wb.py follow-up file, then check again"]),
                         self.merge_check())
        self.follow_ups({'key': 'r2-m1', 'severity': 'minor', 'origin': 'review r2', 'url': 'https://github.com/o/r/issues/9'})
        self.assertEqual((0, ['ok']), self.merge_check())
        self.follow_ups({'key': 'r2-M1', 'severity': 'major', 'origin': 'review r2', 'url': 'https://github.com/o/r/issues/9'})
        code, lines = self.merge_check()
        self.assertEqual(1, code)
        self.assertIn("the major finding 'r2-M1' is deferred", lines[0])

    def test_a_later_clean_round_does_not_ungate_a_stops_minors(self):
        # r1 m1
        self.decision(2, revmux_report([('Minor', 2)]))
        self.decision(3, revmux_report(no_findings=True))
        self.follow_ups({'key': 'r2-m1', 'severity': 'minor', 'origin': 'review r2'})
        self.assertEqual((1, ["follow-up: 'r2-m1' is not filed yet - run wb.py follow-up file, then check again"]),
                         self.merge_check())

    def test_merge_check_on_continue_and_cap(self):
        # AC8
        self.decision(1, revmux_report([('Minor', 1)], ('rate limited: x',)))
        self.assertEqual((1, ['review: round 1 was degraded; another revmux round is due']), self.merge_check())
        self.configure({'minRounds': 3})
        self.decision(2, revmux_report([('Minor', 1)]))
        self.assertEqual((1, ['review: round 2 decided continue (stopWhenNoMajor=true, minRounds=3); '
                              'another revmux round is due']), self.merge_check())
        self.configure({})
        self.decision(5, revmux_report([('Major', 1)]))
        self.assertEqual((1, ["review: round 5 had a Major at the 5-round cap - this is the human's"]), self.merge_check())
        self.decision(5, revmux_report([('Minor', 1)], ('failed',)))
        self.assertEqual((1, ["review: round 5 ended at the 5-round cap (degraded) - this is the human's"]), self.merge_check())
        # With the setting off, the cap is today's rule: condition 1 stays the planner's check.
        self.configure({'stopWhenNoMajor': False})
        self.decision(5, revmux_report([('Major', 1)]))
        self.assertEqual((0, ['ok']), self.merge_check())
        # The decision was frozen into the entry: a later config change does not reinterpret it.
        self.configure({'stopWhenNoMajor': True})
        self.assertEqual((0, ['ok']), self.merge_check())

    def test_a_report_without_a_recorded_decision_fails_the_gate(self):
        self.assertEqual((0, ['ok']), self.merge_check())                     # neither: no revmux round ran
        (self.reviews / 'revmux-r1.md').write_text(revmux_report([('Minor', 1)]), encoding='utf-8')
        self.assertEqual((1, ['review: revmux round 1 has no recorded decision - run wb.py review-round --round 1']),
                         self.merge_check())
        self.decision(1, revmux_report(no_findings=True))
        self.assertEqual((0, ['ok']), self.merge_check())
        (self.reviews / 'revmux-r2.md').write_text(revmux_report([('Minor', 1)]), encoding='utf-8')
        self.assertEqual((1, ['review: revmux round 2 has no recorded decision - run wb.py review-round --round 2']),
                         self.merge_check())


def git(folder, *args):
    return subprocess.run(['git', '-C', str(folder), '-c', 'user.name=t', '-c', 'user.email=t@t', *args],
                          check=True, capture_output=True, text=True).stdout


class ReviewLimit(unittest.TestCase):
    """#77: in a checkout that waits out usage limits, a revmux round a reviewer's usage limit degraded
    decides `limit`: no review, not counted toward the cap, refused by merge-check until it is rerun."""
    setUp, configure, wb, record, decision, entries, merge_check = (
        ReviewRound.setUp, ReviewRound.configure, ReviewRound.wb, ReviewRound.record, ReviewRound.decision,
        ReviewRound.entries, ReviewRound.merge_check)

    LIMITED = revmux_report([('Minor', 1)], statuses=('ok', 'degraded', 'ok', 'ok'))

    def on_limit(self, value='wait'):
        (self.state / 'implementer.json').write_text(json.dumps({'tool': 'kimi', 'onLimit': value}), encoding='utf-8')

    def run_dir(self, round_, events, run=None):
        """The revmux run behind revmux-r<K>.md, as run-revmux.ps1 records it."""
        run = run or f'r{round_}'
        directory = self.folder / 'revmux-tasks' / run
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'events.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in events), encoding='utf-8')
        # revmux's agents are lens groups; the manifest names each one's executor (kimi-mixed's shape).
        (directory / 'manifest.json').write_text(json.dumps({'run': run, 'agents': [
            {'name': 'bugs+impl', 'executor': 'claude'}, {'name': 'adversarial', 'executor': 'kimi'}]}),
            encoding='utf-8')
        (self.reviews / f'revmux-r{round_}.json').write_text(json.dumps(
            {'run': run, 'dir': str(directory), 'profile': 'kimi-mixed', 'attempt': 0,
             'scope': str(self.folder / 'scope.md')}), encoding='utf-8')

    RATE_LIMITED = [{'kind': 'stage', 'text': 'find'},
                    {'kind': 'agent_degraded', 'agent': 'adversarial',
                     'text': "agent adversarial rate limited: you've reached your 5-hour usage limit"}]
    STALLED = [{'kind': 'agent_degraded', 'agent': 'claude-a', 'text': 'agent claude-a stalled'}]

    def review_limit(self):
        path = self.state / 'review-limit.json'
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None

    def test_a_rate_limited_round_in_a_wait_checkout_decides_limit_and_waits(self):
        self.on_limit()
        self.config.write_text(json.dumps({'limitRetryMinutes': 45}), encoding='utf-8')
        self.run_dir(2, self.RATE_LIMITED)
        before = time.time()
        self.assertEqual('review: limit (reviewer usage limit: adversarial)', self.decision(2, self.LIMITED))
        out = self.out.getvalue()
        self.assertIn('revmux --round 2 --rerun --after 45', out)
        self.assertIn('does not count toward the cap', out)
        entry = self.entries()[-1]
        self.assertEqual(('limit', ['adversarial'], True), (entry['decision'], entry['limitedAgents'], entry['degraded']))
        wait = self.review_limit()
        self.assertEqual(('kimi', 2), (wait['tool'], wait['round']))
        self.assertAlmostEqual(before + 45 * 60, wait['retryAt'], delta=5)

    def test_fallback_says_rerun_now_with_claude_only(self):
        self.on_limit()
        self.config.write_text(json.dumps({'reviewOnLimit': 'fallback'}), encoding='utf-8')
        self.run_dir(1, self.RATE_LIMITED)
        self.assertEqual('review: limit (reviewer usage limit: adversarial)', self.decision(1, self.LIMITED))
        self.assertIn('revmux --round 1 --rerun --profile claude-only', self.out.getvalue())
        self.assertIsNone(self.review_limit())

    def test_a_fractional_retry_is_rounded_up_to_whole_minutes(self):
        self.on_limit()
        self.config.write_text(json.dumps({'limitRetryMinutes': 0.5}), encoding='utf-8')
        self.run_dir(1, self.RATE_LIMITED)
        self.decision(1, self.LIMITED)
        self.assertIn('--rerun --after 1 ', self.out.getvalue())

    def test_the_tool_comes_from_the_manifest_not_the_lens_group_name(self):
        # FIX r3 m2: `adversarial` names no tool; the manifest says it ran on kimi.
        self.on_limit()
        self.run_dir(2, self.RATE_LIMITED)
        self.decision(2, self.LIMITED)
        self.assertEqual('kimi', self.review_limit()['tool'])
        claude_group = [{'kind': 'agent_degraded', 'agent': 'bugs+impl', 'text': 'agent bugs+impl rate limited: rejected'}]
        self.run_dir(3, claude_group)
        self.decision(3, self.LIMITED)
        self.assertEqual('claude', self.review_limit()['tool'])
        # Not in the manifest (synthesis), or no readable manifest: the name is the only clue.
        synthesis = [{'kind': 'agent_degraded', 'agent': 'kimi synthesis', 'text': 'agent kimi synthesis rate limited: 403'}]
        self.run_dir(4, synthesis)
        self.decision(4, self.LIMITED)
        self.assertEqual('kimi', self.review_limit()['tool'])
        self.run_dir(5, claude_group)
        (self.folder / 'revmux-tasks' / 'r5' / 'manifest.json').write_text('not json', encoding='utf-8')
        self.decision(5, self.LIMITED)
        self.assertEqual('bugs+impl', self.review_limit()['tool'])

    def test_failover_and_other_degradations_decide_as_before(self):
        self.run_dir(1, self.RATE_LIMITED)
        self.on_limit('failover')
        self.assertEqual('review: continue (degraded)', self.decision(1, self.LIMITED))
        self.on_limit()
        self.run_dir(1, self.STALLED)
        self.assertEqual('review: continue (degraded)', self.decision(1, self.LIMITED))
        (self.reviews / 'revmux-r1.json').unlink()               # no run record: nothing to read
        self.assertEqual('review: continue (degraded)', self.decision(1, self.LIMITED))
        self.assertIsNone(self.review_limit())

    def test_a_limit_at_the_cap_is_still_a_limit(self):
        self.on_limit()
        self.run_dir(5, self.RATE_LIMITED)
        self.assertEqual('review: limit (reviewer usage limit: adversarial)', self.decision(5, self.LIMITED))

    def test_merge_check_refuses_a_limit_until_the_rerun_is_recorded(self):
        self.on_limit()
        self.run_dir(2, self.RATE_LIMITED)
        self.decision(2, self.LIMITED)
        code, lines = self.merge_check()
        self.assertEqual(1, code)
        self.assertIn('review: round 2 hit a reviewer usage limit (adversarial); rerun it (wb.py revmux --round 2 '
                      '--rerun), then run wb.py review-round --round 2 on the rerun\'s report', lines)
        self.assertEqual(1, self.wb('review-round', '--summary'))
        self.assertIn('rerun it first', self.out.getvalue())
        # The rerun: same K, its report replaces the limited one, and its decision replaces `limit`.
        self.run_dir(2, [], run='r2-1')
        self.assertEqual('review: clean', self.decision(2, revmux_report(no_findings=True)))
        self.assertEqual(['clean'], [e['decision'] for e in self.entries()])
        self.assertIsNone(self.review_limit())
        self.assertEqual((0, ['ok']), self.merge_check())

    def test_a_decision_for_an_earlier_round_keeps_the_wait_a_later_one_ends_it(self):
        self.on_limit()
        self.run_dir(2, self.RATE_LIMITED)
        self.decision(2, self.LIMITED)
        self.decision(1, revmux_report([('Major', 1)]))
        self.assertEqual(2, self.review_limit()['round'])
        # FIX r1 M3: a round-K wait is over once round K+1 is recorded.
        self.decision(3, revmux_report([('Minor', 1)]))
        self.assertIsNone(self.review_limit())


class RevmuxRerun(unittest.TestCase):
    """#77: wb.py revmux --rerun reruns round K under a new revmux run name, keeping the limited report."""
    setUp = RevmuxProfile.setUp

    def revmux(self, *args):
        with patch.object(sys, 'argv', ['wb.py', 'revmux', '--round', '2', *args]):
            return wb.main()

    def test_a_rerun_keeps_the_limited_report_and_uses_a_new_run_name(self):
        review = self.folder / '.workbench/review'
        review.mkdir()
        (review / 'revmux-r2.md').write_text('limited', encoding='utf-8')
        (review / 'revmux-r2.json').write_text(json.dumps({'run': 'r2', 'dir': 'x', 'scope': str(self.folder / 'scope.md')}),
                                               encoding='utf-8')
        self.assertEqual(0, self.revmux('--rerun', '--after', '30'))
        command = launched(self.opened)
        self.assertEqual('#20 revmux r2', self.opened.call_args.args[0])
        for part in ("-Round '2'", "-Run 'r2-1'", "-Attempt '1'", "-After '30'", f"-ScopeFile '{self.folder / 'scope.md'}'"):
            self.assertIn(part, command)
        self.assertEqual('limited', (review / 'revmux-r2-limited-1.md').read_text(encoding='utf-8'))
        self.assertTrue((review / 'revmux-r2-limited-1.json').exists())
        self.assertFalse((review / 'revmux-r2.md').exists())
        (review / 'revmux-r2.md').write_text('limited again', encoding='utf-8')
        self.assertEqual(0, self.revmux('--rerun', '--scope', 'scope.md', '--profile', 'claude-only'))
        command = launched(self.opened)
        for part in ("-Run 'r2-2'", "-Attempt '2'", "-Profile 'claude-only'"):
            self.assertIn(part, command)
        self.assertNotIn('-After', command)
        self.assertTrue((review / 'revmux-r2-limited-2.md').exists())

    def limited_round(self):
        review = self.folder / '.workbench/review'
        review.mkdir()
        (review / 'revmux-r2.md').write_text('limited', encoding='utf-8')
        (review / 'revmux-r2.json').write_text(json.dumps({'run': 'r2', 'dir': 'x', 'scope': str(self.folder / 'scope.md')}),
                                               encoding='utf-8')
        return review

    def test_a_session_that_does_not_start_leaves_the_files_as_they_were(self):
        # FIX r1 M1: the limited report is set aside only when the session starts.
        review = self.limited_round()
        self.opened.side_effect = agw.CtlError('no pipe')
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(1, self.revmux('--rerun', '--after', '30'))
        self.assertEqual(['revmux-r2.json', 'revmux-r2.md'], sorted(p.name for p in review.iterdir()))
        with self.assertRaises(SystemExit):
            self.revmux('--rerun', '--scope', 'missing.md')
        self.assertEqual(['revmux-r2.json', 'revmux-r2.md'], sorted(p.name for p in review.iterdir()))

    def test_a_rerun_that_died_before_its_report_is_rerun_under_a_new_name(self):
        # FIX r1 M1: rerun 1 started, wrote its run record, and never produced a report.
        review = self.limited_round()
        self.assertEqual(0, self.revmux('--rerun'))
        (review / 'revmux-r2.json').write_text(json.dumps({'run': 'r2-1', 'dir': 'y'}), encoding='utf-8')
        self.assertEqual(0, self.revmux('--rerun'))
        command = launched(self.opened)
        for part in ("-Run 'r2-2'", "-Attempt '2'", f"-ScopeFile '{self.folder / 'scope.md'}'"):
            self.assertIn(part, command)
        self.assertEqual(['revmux-r2-limited-1.json', 'revmux-r2-limited-1.md', 'revmux-r2.json'],
                         sorted(p.name for p in review.iterdir()))
        # FIX r2 M1: r2-2 ran and decided `limit` again; the next rerun must not reuse r2-2.
        (review / 'revmux-r2.md').write_text('limited again', encoding='utf-8')
        (review / 'revmux-r2.json').write_text(json.dumps({'run': 'r2-2', 'dir': 'z', 'attempt': 2,
                                                           'scope': str(self.folder / 'scope.md')}), encoding='utf-8')
        self.assertEqual(0, self.revmux('--rerun'))
        self.assertIn("-Run 'r2-3'", launched(self.opened))
        self.assertIn("-Attempt '3'", launched(self.opened))
        self.assertTrue((review / 'revmux-r2-limited-3.md').exists())

    def test_rerun_refusals(self):
        with self.assertRaises(SystemExit) as caught:
            self.revmux('--scope', 'scope.md', '--after', '30')
        self.assertIn('--after goes with --rerun', str(caught.exception))
        with self.assertRaises(SystemExit) as caught:
            self.revmux('--rerun', '--scope', 'scope.md')
        self.assertIn('no report to rerun', str(caught.exception))
        with self.assertRaises(SystemExit) as caught:
            self.revmux()
        self.assertIn('needs --scope', str(caught.exception))
        self.opened.assert_not_called()

    def test_a_first_round_passes_no_run_name(self):
        self.assertEqual(0, self.revmux('--scope', 'scope.md'))
        self.assertNotIn('-Run', launched(self.opened))


class ReviewCapSources(unittest.TestCase):
    """#75: the diff size and the issue's title and labels, read live."""

    def setUp(self):
        self.folder = Path(__file__).resolve().parent.parent / ('test wb cap ' + uuid.uuid4().hex)
        self.folder.mkdir()
        self.addCleanup(remove_tree, self.folder)
        git(self.folder, 'init', '-q', '-b', 'main')

    def commit(self, files, message):
        for name, content in files.items():
            path = self.folder / name
            if content is None:
                path.unlink()
            elif isinstance(content, bytes):
                path.write_bytes(content)
            else:
                path.write_text(content, encoding='utf-8')
        git(self.folder, 'add', '-A')
        git(self.folder, 'commit', '-q', '-m', message)

    def test_diff_lines_counts_the_branch_against_its_base(self):
        self.commit({'a.txt': 'one\ntwo\nthree\n', 'old.txt': 'x\ny\n'}, 'base')
        self.assertIsNone(wb.diff_lines(self.folder))                  # no origin ref: unmeasurable
        git(self.folder, 'update-ref', 'refs/remotes/origin/main', 'HEAD')
        git(self.folder, 'checkout', '-q', '-b', 'issue-75-x')
        self.commit({'a.txt': 'one\nTWO\nthree\nfour\n', 'old.txt': None, 'b.bin': b'\x00\x01\x02' * 50}, 'work')
        # a.txt: 2 added, 1 deleted; old.txt: 2 deleted; the binary counts 0
        self.assertEqual((5, 'origin/main'), wb.diff_lines(self.folder))
        # the default branch moving on (and merged in) does not count: the diff is from the merge-base
        git(self.folder, 'checkout', '-q', 'main')
        self.commit({'main.txt': 'm\n' * 40}, 'main moves')
        git(self.folder, 'update-ref', 'refs/remotes/origin/main', 'HEAD')
        git(self.folder, 'checkout', '-q', 'issue-75-x')
        git(self.folder, 'merge', '-q', '--no-ff', '-m', 'update', 'main')
        self.assertEqual((5, 'origin/main'), wb.diff_lines(self.folder))
        # origin/HEAD names the base when it is set
        git(self.folder, 'update-ref', 'refs/remotes/origin/trunk', 'main')
        git(self.folder, 'symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/trunk')
        self.assertEqual((5, 'origin/trunk'), wb.diff_lines(self.folder))

    def test_diff_lines_reads_only_the_checkouts_own_git(self):
        # the enclosing repo is measurable, so without --git-dir git would find it and count its diff
        self.commit({'a.txt': 'one\n'}, 'base')
        git(self.folder, 'update-ref', 'refs/remotes/origin/main', 'HEAD')
        self.commit({'a.txt': 'one\ntwo\n'}, 'work')
        self.assertEqual((1, 'origin/main'), wb.diff_lines(self.folder))
        inner = self.folder / 'not a checkout'
        inner.mkdir()
        self.assertIsNone(wb.diff_lines(inner))

    def test_issue_facts_never_exits(self):
        self.commit({'a.txt': 'a\n'}, 'base')
        git(self.folder, 'checkout', '-q', '-b', 'issue-75-x')
        with patch.object(wb, 'gh_run', side_effect=AssertionError('gh called')):
            self.assertIsNone(wb.issue_facts(self.folder))            # no origin
            git(self.folder, 'remote', 'add', 'origin', 'https://gitlab.com/o/r.git')
            self.assertIsNone(wb.issue_facts(self.folder))            # not GitHub: no exit 2
        git(self.folder, 'remote', 'set-url', 'origin', 'https://github.com/o/r.git')
        answer = json.dumps({'title': 'Batch: x', 'labels': [{'name': 'big'}, {'name': 'bug'}]})
        with patch.object(wb, 'gh_run', return_value=subprocess.CompletedProcess([], 0, answer, '')) as gh:
            self.assertEqual({'title': 'Batch: x', 'labels': ['big', 'bug']}, wb.issue_facts(self.folder))
        self.assertEqual(('issue', 'view', '75', '--json', 'title,labels'), gh.call_args.args[1:])
        for done in (subprocess.CompletedProcess([], 1, '', 'HTTP 502'), subprocess.CompletedProcess([], 0, 'nope', '')):
            with patch.object(wb, 'gh_run', return_value=done):
                self.assertIsNone(wb.issue_facts(self.folder))
        with patch.object(wb, 'gh_run', side_effect=OSError('no gh')):
            self.assertIsNone(wb.issue_facts(self.folder))
        git(self.folder, 'checkout', '-q', '-b', 'feature')
        with patch.object(wb, 'gh_run', side_effect=AssertionError('gh called')):
            self.assertIsNone(wb.issue_facts(self.folder))            # not an issue branch


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
                       '--match-head-commit <full sha>', 'None is deferred', 'within the review cap',
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

    def test_small_conflicts_and_the_merge_note(self):
        # #90
        root = Path(__file__).resolve().parent.parent
        text = (root / 'claude/commands/start-github-issue.md').read_text(encoding='utf-8')
        auto = ' '.join(text.split('### Auto-merge')[1].split('### The human')[0].split())
        for needle in ['`(small: ...; not counted)`', '`(counted: <reason>)`', '`mergeRounds.smallConflictHunks`',
                       'touches no file the review flagged', "merge-round reads update-check's record for the HEAD",
                       'A kind that contradicts the record exits 2', 'a fourth counted conflict',
                       'a small conflict is never refused', 'say **blocked** in the PR comment and in chat',
                       "merge-round's refusal line verbatim", '<the `wb.py merge-round --pr <N> --summary` line>',
                       '`merge rounds: 1 clean update; conflicts: 2 small (uncounted), 1 counted of 3`']:
            self.assertIn(needle, auto)
        self.assertNotIn('a second conflict', auto)
        readme = ' '.join((root / 'README.md').read_text(encoding='utf-8').split())
        for needle in ['3 counted conflict rounds (`mergeRounds.conflict`)', 'A **small** conflict is not counted at all',
                       'at most 3 conflict hunks (`mergeRounds.smallConflictHunks`; 0 turns small conflicts off)',
                       'A line added right beside a conflict cannot be told apart from its resolution',
                       'the planner never declares a conflict small itself', 'Each merge commit is counted once',
                       '`wb.py merge-round --pr <N> --summary`',
                       '| `mergeRounds` | `{"conflict": 3, "smallConflictHunks": 3}` |']:
            self.assertIn(needle, readme)
        self.assertNotIn('1 conflict round', readme)

    def test_both_implementers_know_update(self):
        for path in ('claude/commands/workbench-implementer.md', 'codex/skills/workbench-implementer/SKILL.md'):
            with self.subTest(path=path):
                text = ' '.join((Path(__file__).resolve().parent.parent / path).read_text(encoding='utf-8').split())
                for needle in ['## UPDATE - bring the branch up to date', '`git merge --no-ff <base sha>`',
                               '**Never rebase, never `git pull`, never amend, squash or force-push**',
                               'Add nothing else to the merge commit', 'Reply `UPDATED <sha>`',
                               '`git merge --abort` and reply `CANNOT-RESOLVE <why>`', 'update-check',
                               'Change only the conflict regions: editing or removing any line outside them makes it a counted conflict round',
                               'A line added right beside a region cannot be told apart from the resolution']:   # #90
                    self.assertIn(needle, text)
                self.assertNotIn('even one line beside a conflict', text)          # r2 m2: more than the code checks

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
                       'Never answer the chooser: fail over', 'failover=false', 'only a record', 'never types into the limited agent']:
            self.assertIn(needle, section)

    def test_both_implementers_know_handover(self):
        for path in ('claude/commands/workbench-implementer.md', 'codex/skills/workbench-implementer/SKILL.md'):
            with self.subTest(path=path):
                text = self.text(path)
                self.assertIn('## HANDOVER - you replace another implementer mid-loop', text)
                self.assertIn('Run `git status`', text)
                self.assertIn('Continue the phase the mail names', text)


class FakeGh:
    """gh at the subprocess boundary for follow-up filing. Like gh in a fork's checkout (#71), a call
    that does not name the repo is answered from the upstream: run_wb fails on it."""

    def __init__(self, source_labels=(), open_issues=(), label_fails=False, create_fails=(), repo='o/r'):
        self.repo = repo
        self.unscoped = []
        self.source_labels = list(source_labels)
        self.open_issues = list(open_issues)
        self.label_fails = label_fails
        self.create_fails = set(create_fails)
        self.calls = []
        self.bodies = {}
        self.next = 100

    def __call__(self, argv, **kwargs):
        done = lambda out='', code=0, err='': subprocess.CompletedProcess(argv, code, out, err)
        if argv[0] == 'git':
            assert argv[-3:] == ['remote', 'get-url', 'origin'], argv
            return done(f'https://github.com/{self.repo}.git\n')
        self.calls.append(argv)
        args = argv[1:]
        if '--repo' not in args or args[args.index('--repo') + 1] != self.repo:
            self.unscoped.append(argv)
            if args[:2] == ['issue', 'view']:
                return done(json.dumps({'title': 'build(deps): an upstream pull request', 'labels': []}))
        if args[:2] == ['issue', 'view']:
            return done(json.dumps({'title': 'Source issue',
                                    'labels': [{'name': n} for n in self.source_labels]}))
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
            return done(f'https://github.com/{self.repo}/issues/{self.next}\n')
        raise AssertionError(argv)


class FollowUps(unittest.TestCase):
    """#27: follow-ups are recorded, deduped and filed before the merge; merge-check gates on them.
    These pin #27's per-item filing where it still applies and #58's shared leftovers path.
    tests/test_followup.py covers dedupe on."""

    def setUp(self):
        self.folder = Path(__file__).resolve().parent.parent / ('test wb follow ' + uuid.uuid4().hex)
        self.state = self.folder / '.workbench/state'
        self.state.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.folder)
        self.addCleanup(hub.reload_paths)
        config = self.folder / 'agworkbench.json'
        config.write_text(json.dumps({'followUp': {'dedupe': False}}), encoding='utf-8')
        self.enterContext(patch.dict(os.environ, {'AI_HUB': str(self.folder / '.workbench'),
                                                 'AGWORKBENCH_CONFIG': str(config)}))
        hub.reload_paths()
        self.out, self.err = io.StringIO(), io.StringIO()
        self.enterContext(contextlib.redirect_stdout(self.out))
        self.enterContext(contextlib.redirect_stderr(self.err))

    def run_wb(self, *argv, gh=None):
        with patch.object(sys, 'argv', ['wb.py', *argv]), \
                patch.object(wb.subprocess, 'run', side_effect=gh or AssertionError('gh called')):
            code = wb.main()
        self.assertEqual([], getattr(gh, 'unscoped', []), 'a gh call left the repo to gh (#71)')
        return code

    def add(self, key, severity='minor', disputed=False, title=None, own_issue=False):
        body = self.folder / f'{key}.md'
        body.write_text(f'evidence for {key}: lib/x.py:12 fails', encoding='utf-8')
        argv = ['follow-up', 'add', '--key', key, '--title', title or f'Fix {key}', '--body-file', str(body),
                '--severity', severity, '--origin', 'review r2']
        self.assertEqual(0, self.run_wb(*argv + (['--disputed'] if disputed else [])
                                        + (['--own-issue'] if own_issue else [])))

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
        self.add('r2-m1', own_issue=True)
        self.add('plan-queue', severity='plan', own_issue=True)
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

    def test_dedupe_off_makes_exactly_the_27_gh_calls(self):
        # Without a PR, dedupe off preserves #27's per-item calls and body - in a fork's checkout, each
        # naming the fork (#71: gh's own resolution picks the parent there).
        self.add('r2-m1', title='Fix the relay')
        gh = FakeGh(repo='fork/r')
        self.assertEqual(0, self.run_wb('follow-up', 'file', '--source', '27', gh=gh))
        body = str(self.state / 'follow-up-r2-m1.md')
        self.assertEqual([['gh', 'issue', 'view', '27', '--json', 'title,labels', '--repo', 'fork/r'],
                          ['gh', 'label', 'create', 'follow-up', '--color', 'BFD4F2',
                           '--description', 'filed automatically by an agworkbench loop', '--repo', 'fork/r'],
                          ['gh', 'issue', 'list', '--state', 'open', '--search', '"Fix the relay" in:title',
                           '--json', 'title,url', '--limit', '200', '--repo', 'fork/r'],
                          ['gh', 'issue', 'create', '--title', 'Fix the relay', '--body-file', body, '--label', 'follow-up',
                           '--repo', 'fork/r']],
                         gh.calls)
        self.assertEqual('https://github.com/fork/r/issues/101', self.items()[0]['url'])
        self.assertEqual('evidence for r2-m1: lib/x.py:12 fails\n\nSource: #27\nSeverity: minor; origin: review r2\n\n'
                         '<!-- agworkbench:follow-up source=#27 -->\n<!-- agworkbench:planner -->\n',
                         gh.bodies['Fix the relay'])

    def test_dedupe_off_with_pr_creates_one_leftovers_issue(self):
        self.add('r2-m1')
        self.add('plan-queue', severity='plan')
        gh = FakeGh(repo='fork/r')
        self.assertEqual(0, self.run_wb('follow-up', 'file', '--source', '27', '--pr', '30', gh=gh))
        # #71: in a fork's checkout, the title is the fork's #27 and every call names the fork -
        # no `gh repo view`, which there answers with the parent.
        self.assertEqual([['gh', 'issue', 'view', '27', '--json', 'title,labels', '--repo', 'fork/r'],
                          ['gh', 'label', 'create', 'follow-up', '--color', 'BFD4F2',
                           '--description', 'filed automatically by an agworkbench loop', '--repo', 'fork/r'],
                          ['gh', 'issue', 'list', '--state', 'open', '--search',
                           '"Leftovers from #27: Source issue" in:title', '--json', 'number,title', '--limit', '200',
                           '--repo', 'fork/r'],
                          ['gh', 'label', 'create', 'priority:P2', '--color', 'D93F0B',
                           '--description', 'agworkbench priority (#34, #42)', '--repo', 'fork/r'],
                          ['gh', 'issue', 'create', '--title', 'Leftovers from #27: Source issue',
                           '--body-file', str(self.state / 'follow-up-leftovers.md'),
                           '--label', 'follow-up', '--label', 'priority:P2', '--repo', 'fork/r']], gh.calls)
        self.assertEqual('https://github.com/fork/r/issues/101', self.items()[0]['url'])
        body = gh.bodies['Leftovers from #27: Source issue']
        self.assertIn('- [ ] **r2-m1**', body)
        self.assertIn('- [ ] **plan-queue**', body)
        self.assertEqual(self.items()[0]['url'], self.items()[1]['url'])

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
        with patch.object(wb, 'issue_facts', return_value=None), patch.object(wb, 'diff_lines', return_value=None):
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
                       'merge comment** lists the leftovers issue once with its checklist items, every separate',
                       'follow-up URL', 'loop-state done --pr <P> --sha <merged sha>',
                       'never closes on a timeout', 'brakes are unchanged']:
            self.assertIn(needle, section)
        self.assertIn('deferred **with a filed follow-up issue**; a Major or blocker never may, disputed or not',
                      text.split('## Phase 6')[1])
        self.assertIn('loop-state done --pr <P> --sha <sha>` as the very last step', text.split('## Phase 7')[1])


KIMI_PLAN = """# Plan v1 for #616

## Goal
Keep sheet edits on save.

## Exact edits
- `crates/xlsx/src/save.rs` `save_sheets`: guard only the `splice_worksheet` call, not the rest of the loop.

## Must not change
- the per-sheet loop's other steps; `crates/xlsx/src/read.rs`.

## Oracle
- `tests/fixtures/macro-sheet.xlsm` and `crates/xlsx/tests/roundtrip.rs` (sibling code to mirror).

## Tests first
- `macro_sheet_edit_survives_save` fails today (a side effect: the edit is lost on save).

## Pitfalls
- the save path is shared with the chart writer.

## Done means
- `cargo fmt`, `cargo clippy -p xlsx -- -D warnings`, `cargo test -p xlsx`; IMPLEMENTED <sha> with test
  names, counts, and every skipped test by name.
"""


class PlanCheck(unittest.TestCase):
    """#77: with a Kimi implementer the plan must be Kimi-grade: `wb.py plan-check` says what is missing."""

    def setUp(self):
        self.folder = Path(__file__).resolve().parent.parent / ('test wb plan ' + uuid.uuid4().hex)
        (self.folder / '.workbench/state').mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.folder)
        self.enterContext(patch.dict(os.environ, {'AI_HUB': str(self.folder / '.workbench')}))
        (self.folder / '.workbench/state/implementer.json').write_text('{"tool": "kimi"}', encoding='utf-8')
        # KIMI_PLAN's oracle: with the checkout known, an oracle path must exist there (FIX r3 m3).
        for name in ('tests/fixtures/macro-sheet.xlsm', 'crates/xlsx/tests/roundtrip.rs'):
            (self.folder / name).parent.mkdir(parents=True, exist_ok=True)
            (self.folder / name).write_text('x', encoding='utf-8')

    def check(self, plan, *extra):
        (self.folder / '.workbench/plan.md').write_text(plan, encoding='utf-8')
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch.object(sys, 'argv', ['wb.py', 'plan-check', *extra]):
            code = wb.main()
        return code, out.getvalue()

    def test_a_kimi_grade_plan_passes(self):
        self.assertEqual((0, 'plan-check: ok\n'), self.check(KIMI_PLAN))

    def test_each_missing_section_is_named(self):
        for name in ('Exact edits', 'Must not change', 'Tests first', 'Pitfalls', 'Done means'):
            with self.subTest(section=name):
                code, out = self.check(KIMI_PLAN.replace(f'## {name}', '## Notes'))
                self.assertEqual(1, code)
                self.assertIn(f'plan-check: missing section: {name}', out)
        code, out = self.check('# Plan\n\n## Goal\nx\n')
        self.assertEqual(1, code)
        self.assertEqual(7, out.count('plan-check: '), out)     # five sections, the oracle, and the verdict

    def test_a_plan_without_an_oracle_path_and_without_stop_is_rejected(self):
        for plan in (KIMI_PLAN.replace('## Oracle', '## Background'),
                     KIMI_PLAN.replace("- `tests/fixtures/macro-sheet.xlsm` and `crates/xlsx/tests/roundtrip.rs` "
                                       "(sibling code to mirror).", '- the usual files')):
            with self.subTest(plan=plan[:0]):
                code, out = self.check(plan)
                self.assertEqual(1, code)
                self.assertIn('no Oracle section naming at least one path, and no STOP AND REPORT line', out)

    def test_stop_and_report_stands_in_for_the_oracle(self):
        plan = KIMI_PLAN.replace('## Oracle', '## Background') + '\nSTOP AND REPORT: the MPX layout is not in the repo.\n'
        self.assertEqual((0, 'plan-check: ok (STOP AND REPORT)\n'), self.check(plan))

    def test_the_stop_rule_restated_is_not_a_stop_verdict(self):
        # FIX r1 M2: the skill asks for the rules where they apply; restating one is not STOP.
        no_oracle = KIMI_PLAN.replace('## Oracle', '## Background')
        plan = no_oracle.replace('- the save path is shared with the chart writer.',
                                 '- field offsets cite a source in the repo. Without one, the plan says `STOP AND REPORT`.')
        code, out = self.check(plan)
        self.assertEqual(1, code)
        self.assertIn('no Oracle section naming at least one path, and no STOP AND REPORT line', out)
        for verdict in ('- **STOP AND REPORT**: the MPX layout is not in the repo.',
                        '`STOP AND REPORT` - no sample .mpx in the repo'):
            with self.subTest(verdict=verdict):
                self.assertEqual((0, 'plan-check: ok (STOP AND REPORT)\n'), self.check(no_oracle + '\n' + verdict + '\n'))
        oracle_stop = KIMI_PLAN.replace(
            "- `tests/fixtures/macro-sheet.xlsm` and `crates/xlsx/tests/roundtrip.rs` (sibling code to mirror).",
            'None in the repo: STOP AND REPORT.')
        self.assertEqual((0, 'plan-check: ok (STOP AND REPORT)\n'), self.check(oracle_stop))

    def test_slash_prose_is_not_an_oracle_path(self):
        # FIX r2 m1: an Oracle must name a file (an extension) or something that exists in the checkout.
        oracle = "- `tests/fixtures/macro-sheet.xlsm` and `crates/xlsx/tests/roundtrip.rs` (sibling code to mirror)."
        for prose in ('None in the repo; the read/write round-trip is new.', 'and/or the I/O layer'):
            with self.subTest(prose=prose):
                code, out = self.check(KIMI_PLAN.replace(oracle, prose))
                self.assertEqual(1, code)
                self.assertIn('no Oracle section naming at least one path', out)
        self.assertEqual(1, self.check(KIMI_PLAN.replace(oracle, '- tests/fixtures/x.docx'))[0])   # not there
        (self.folder / 'tests/fixtures/x.docx').write_text('x', encoding='utf-8')
        self.assertEqual(0, self.check(KIMI_PLAN.replace(oracle, '- tests/fixtures/x.docx'))[0])
        self.assertEqual(0, self.check(KIMI_PLAN.replace(oracle, '- the files in `tests/fixtures/`'))[0])

    def test_an_outside_spec_is_never_an_oracle(self):
        # FIX r3 m3: an outside spec is what Kimi must not implement from (#309).
        oracle = "- `tests/fixtures/macro-sheet.xlsm` and `crates/xlsx/tests/roundtrip.rs` (sibling code to mirror)."
        for outside in ('- https://example.org/spec.pdf', '- example.org/spec.pdf', '- see <https://example.com/spec.pdf>'):
            with self.subTest(outside=outside):
                code, out = self.check(KIMI_PLAN.replace(oracle, outside))
                self.assertEqual(1, code)
                self.assertIn('no Oracle section naming at least one path', out)
        self.assertEqual([], wb.plan_paths('- https://example.org/spec.pdf', None))
        self.assertEqual(['tests/x.docx'], wb.plan_paths('- `tests/x.docx`, read/write', None))   # a direct call

    def test_a_conditional_stop_is_not_a_verdict(self):
        # FIX r2 m2, m3: the rule restated as a bullet, or a condition before the marker in the Oracle.
        no_oracle = KIMI_PLAN.replace('## Oracle', '## Background')
        # FIX r3 m1: the words right after the marker decide, past punctuation, bold and backticks.
        for line in ('- STOP AND REPORT when a field offset has no source in the repo.',
                     '- **STOP AND REPORT** if the corpus is missing.', '- STOP AND REPORT: if the corpus is missing.',
                     '- **STOP AND REPORT** - when unsure.', '- STOP AND REPORT (if the corpus is missing)',
                     '- STOP AND REPORT, when unsure', '- STOP AND REPORT — whenever in doubt'):
            with self.subTest(line=line):
                self.assertEqual(1, self.check(no_oracle + '\n' + line + '\n')[0])
        for line in ('- STOP AND REPORT: the corpus is missing, and if it were here the plan would change.',
                     '- **STOP AND REPORT**: without the MPX layout every offset is a guess.'):
            with self.subTest(line=line):
                self.assertEqual((0, 'plan-check: ok (STOP AND REPORT)\n'), self.check(no_oracle + '\n' + line + '\n'))
        oracle = "- `tests/fixtures/macro-sheet.xlsm` and `crates/xlsx/tests/roundtrip.rs` (sibling code to mirror)."
        # On the Oracle's first line: only the clause right before the marker, and the words after it.
        for first, code in (('None in the repo: STOP AND REPORT.', 0),
                            ('None in the repo; without one every offset is a guess: STOP AND REPORT.', 0),
                            ('Without a sample in the repo, STOP AND REPORT.', 0),
                            ('If the offsets have no source STOP AND REPORT.', 1),
                            ('None in the repo: STOP AND REPORT if unsure.', 1)):
            with self.subTest(first=first):
                self.assertEqual(code, self.check(KIMI_PLAN.replace(oracle, first))[0])

    def test_bold_and_numbered_headings_count(self):
        plan = KIMI_PLAN.replace('## Exact edits', '**Exact edits**').replace('## Pitfalls', '### 4. Pitfalls:')
        self.assertEqual(0, self.check(plan)[0])

    def test_done_means_must_ask_for_skipped_tests(self):
        code, out = self.check(KIMI_PLAN.replace(', and every skipped test by name', ''))
        self.assertEqual(1, code)
        self.assertIn('Done means does not ask for skipped tests by name', out)

    def test_not_required_for_other_implementers_and_another_path(self):
        (self.folder / '.workbench/state/implementer.json').write_text('{"tool": "codex"}', encoding='utf-8')
        self.assertEqual((0, 'plan-check: not required (implementer=codex)\n'), self.check('# nothing'))
        (self.folder / '.workbench/state/implementer.json').write_text('{"tool": "kimi"}', encoding='utf-8')
        (self.folder / 'other.md').write_text(KIMI_PLAN, encoding='utf-8')
        self.assertEqual(0, self.check('# nothing', '--plan', 'other.md')[0])


class KimiPlanProse(unittest.TestCase):
    """#77: the planner's prompt requires the Kimi-grade sections and plan-check; Kimi's role follows them."""

    def text(self, path):
        return ' '.join((Path(__file__).resolve().parent.parent / path).read_text(encoding='utf-8').split())

    def test_the_planner_requires_the_sections_the_rules_and_plan_check(self):
        text = self.text('claude/commands/start-github-issue.md')
        phase2 = text.split('## Phase 2')[1].split('## Phase 3')[0]
        kimi = phase2.split('### When the implementer is Kimi')[1]
        for needle in ['**Exact edits**', 'scope of the edit', '**Must not change**', '**Oracle**', '**by path**',
                       '`STOP AND REPORT`', 'never invent a format or a sample', '**Tests first**',
                       'must fail on the current code', '**side effect**', '**Pitfalls**', '**Done means**',
                       '**every skipped test by name**', 'runs the real-file oracle (the corpus) when one exists',
                       'is lenient: an unknown value falls back to the old behaviour, never to a new hard error',
                       'cites its source in the repo', 'wb.py" plan-check', 'never implemented as a guess',
                       '**The verdict is a line of its own that starts with `STOP AND REPORT`**',
                       'is not a verdict', 'a restated rule never opens its line with the marker',
                       'The path must exist in the checkout: a URL or an outside spec is never an oracle']:
            self.assertIn(needle, kimi)
        self.assertLess(kimi.index('plan-check'), kimi.index('Send it as `plan v1`'))
        phase4 = text.split('## Phase 4')[1].split('## Phase 5')[0]
        for needle in ["compare the diff with the plan's Exact edits and Must not change",
                       "Judge the code, not Kimi's report", "Never merge on Kimi's own report"]:
            self.assertIn(needle, phase4)
        self.assertLess(phase4.index('Exact edits and Must not change'), phase4.index('Launch revmux'))

    def test_the_planner_knows_the_limit_decision_and_the_wait_mode(self):
        text = self.text('claude/commands/start-github-issue.md')
        phase4 = text.split('## Phase 4')[1].split('## Phase 5')[0]
        for needle in ['| `limit` |', 'never counts toward the cap', 'revmux --round <K> --rerun --after <minutes>',
                       '--rerun --profile claude-only', 'run `review-round --round <K>` again']:
            self.assertIn(needle, phase4)
        limits = text.split('## Usage limits')[1].split('## Stall pointers')[0]
        for needle in ['`onLimit=wait`', '**Never fail over and never report blocked**', 'end your turn',
                       '`... waiting, cannot probe`', 'A Codex `warning` chooser is not waited out']:
            self.assertIn(needle, limits)

    def test_kimi_follows_the_plan_literally_and_reports_skipped_tests(self):
        text = self.text('kimi/AGENTS.md')
        for needle in ["**Follow the plan's Exact edits literally**", 'never touch anything under Must not change',
                       '**Never invent a file format, a field id, an offset or a sample.**', '`STOP AND REPORT`',
                       'implement nothing on a guess', '**every skipped test by name**',
                       'a line of the plan that starts with `STOP AND REPORT`, or an Oracle section whose first line carries it',
                       'is not a verdict']:
            self.assertIn(needle, text)

    def test_the_readme_describes_the_slow_queue(self):
        text = self.text('README.md')
        section = text.split('### A slow queue that waits out usage limits')[1].split('### ')[0]
        for needle in ["github-workbench -Queue 'where: kimi AND priority IN [P2, P3]' -Repo yeroo/docxy -QueueName kimi "
                       "-Workspace docxy-kimi -Implementer kimi -WaitOnLimit -Autonomous -Watch",
                       '`limitRetryMinutes`', '`"reviewOnLimit": "fallback"`', '`waiting-limit (active)`',
                       '`kimi` label', 'wb.py plan-check']:
            self.assertIn(needle, section)


class ReviewRoundProse(unittest.TestCase):
    """#64: the planner records each round's decision and stops at a round with no Major; the
    implementers know a final fix round has no revmux round after it."""

    def text(self, path):
        return ' '.join((Path(__file__).resolve().parent.parent / path).read_text(encoding='utf-8').split())

    def test_planner_phase_4_to_6(self):
        text = self.text('claude/commands/start-github-issue.md')
        phase4 = text.split('## Phase 4')[1].split('## Phase 5')[0]
        for needle in ['wb.py" review-round --round <K>', '--severe <n> --reason "<why>"',
                       "Never go below revmux's count without a reason", '| `continue` |', '| `stop` |', '| `clean` |',
                       '| `cap` |', '`FIX r<K> (final)`', '--origin "review r<K>"',
                       '**Review stops once a round has no Major**', 'no further revmux round runs',
                       'A round with a Major gets another round after its fix', "At most the review cap's revmux rounds",
                       '**The review cap** is `review.maxRounds` (5), or `review.maxRoundsBig` (10) for a **big** issue',
                       '`Batch:`', '`-BigReview`', 'Judged big once, an issue stays big', '`reviewCap=<n>`']:
            self.assertIn(needle, phase4)
        self.assertLess(phase4.index('review-round --round <K>'), phase4.index('Send the verified findings'))
        self.assertIn('The difference counts as Minor findings, so the round stops rather than reads clean', phase4)
        phase5 = text.split('## Phase 5')[1].split('## Phase 6')[0]
        # r1 M2: without auto-merge nothing later files a stop's minors, so they are filed with the PR.
        for needle in ['wb.py review-round --summary', 'leave it out when it exits 2',
                       "**A stop's deferred minors are filed now**, with or without auto-merge",
                       'wb.py" follow-up file --source <N> --pr <P>`, run `review-round --summary` again',
                       'gh pr edit <P> --repo <owner/repo> --body-file .workbench/pr-body.md']:
            self.assertIn(needle, phase5)
        self.assertLess(phase5.index('gh pr create'), phase5.index('gh pr edit <P>'))
        self.assertNotIn('filed at merge', text)
        for stale in ('five-round', 'At most five', 'round 5 or later', 'FIX r5', 'round 6 and later'):
            self.assertNotIn(stale, text)        # #75: the cap is the review cap, five or ten
        phase6 = text.split('## Phase 6')[1].split('## Phase 7')[0]
        for needle in ['A review that **stopped**', 'fixed or recorded as a follow-up',
                       '`follow-up file --source <N> --pr <P>`) before merge-check, with or without autonomy',
                       'the newest revmux report when it has no recorded decision', '<the `wb.py review-round --summary` line>',
                       'review stopped: round K had no Major; N minor finding(s) in <leftovers URL>',
                       'recorded with `review-round` like any other', 'rounds past the review cap included',
                       'within the review cap', 'a degraded run at or past the review cap',
                       'unless it was recorded with `stopWhenNoMajor: false`']:
            self.assertIn(needle, phase6)

    def test_follow_ups_are_reachable_without_autonomy(self):
        text = self.text('claude/commands/start-github-issue.md')
        section = text.split('## Follow-ups')[1].split('## ')[0]
        self.assertIn('With or without autonomy', section)
        for needle in ['wb.py" follow-up add --key', 'follow-up file --source <N> --pr <P>', 'never lower it below revmux',
                       '**File them as soon as the PR exists**']:
            self.assertIn(needle, section)
        self.assertIn('as "Follow-ups" below says', text.split('## Full autonomy')[1].split('## Follow-ups')[0])

    def test_both_implementers_know_the_final_round(self):
        for path in ('claude/commands/workbench-implementer.md', 'codex/skills/workbench-implementer/SKILL.md'):
            with self.subTest(path=path):
                text = self.text(path)
                self.assertIn('A `FIX r<K> (final)` round has no revmux round after it', text)
                self.assertIn('Fix the cheap Minor and Immaterial findings; mark the rest `deferred` with a reason', text)


class LoopEndProse(unittest.TestCase):
    """#27 r18b: an implementer does not answer the end of the loop, so no unread reply blocks the close."""

    def test_both_implementers_stop_without_replying(self):
        root = Path(__file__).resolve().parent.parent
        for path in ('claude/commands/workbench-implementer.md', 'codex/skills/workbench-implementer/SKILL.md'):
            with self.subTest(path=path):
                text = ' '.join((root / path).read_text(encoding='utf-8').split())
                self.assertIn('When mail says the loop is complete, or the relay reports the PR MERGED or CLOSED', text)
                self.assertIn('do not reply', text)


class ForkGh:
    """gh in a checkout of fork/r, a fork of up/r (#71): gh's own base resolution picks the parent, so
    a call that does not name fork/r is recorded as unscoped (and answered as the parent would)."""

    def __init__(self, repo='fork/r', pr_repo=None):
        self.repo, self.pr_repo = repo, pr_repo or repo
        self.calls, self.unscoped = [], []

    def names_repo(self, args):
        if args[0] == 'api':
            return any(a.startswith(f'repos/{self.repo}/') for a in args)
        return '--repo' in args and args[args.index('--repo') + 1] == self.repo

    def __call__(self, argv, **kwargs):
        done = lambda out='', code=0, err='': subprocess.CompletedProcess(argv, code, out, err)
        if argv[0] == 'git':
            if argv[-3:] == ['remote', 'get-url', 'origin']:
                return done(f'git@github.com:{self.repo}.git\n')
            if argv[-2:] == ['branch', '--show-current']:
                return done('issue-1-fix\n')
            raise AssertionError(argv)
        self.calls.append(argv)
        args = argv[1:]
        if not self.names_repo(args):
            self.unscoped.append(argv)
        link = f'https://github.com/{self.repo}/actions/runs/11/job/22'
        if args[:2] == ['pr', 'view']:
            return done(json.dumps(clean_pr(url=f'https://github.com/{self.pr_repo}/pull/7', mergeStateStatus='BLOCKED')))
        if args[:1] == ['api']:
            return done(json.dumps([[]]))
        if args[:2] == ['pr', 'checks']:
            return done(json.dumps([] if '--required' in args else [check('build', 'fail', link=link)]), code=1)
        if args[:2] == ['run', 'view']:
            return done('the failing line\n')
        if args[:2] == ['run', 'rerun']:
            return done()
        if args[:2] == ['issue', 'view']:
            return done(json.dumps({'state': 'CLOSED', 'stateReason': 'COMPLETED'}))
        if args[:2] == ['pr', 'list']:
            return done('[]')
        raise AssertionError(argv)


class ForkCheckout(unittest.TestCase):
    """#71: in a fork's checkout gh resolves its base repository to the parent. Every workbench gh call
    names the checkout's own repo, and a write to another repo is refused before gh runs."""

    def setUp(self):
        self.folder = scratch(self, 'wb-fork-')
        self.state = self.folder / '.workbench/state'
        self.state.mkdir(parents=True)
        self.addCleanup(hub.reload_paths)
        self.enterContext(patch.dict(os.environ, {'AI_HUB': str(self.folder / '.workbench'), 'AI_BOX': 'claude'}))
        hub.reload_paths()
        (self.state / 'relay.json').write_text(json.dumps({'seen_open': [7]}), encoding='utf-8')
        self.out, self.err = io.StringIO(), io.StringIO()
        self.enterContext(contextlib.redirect_stdout(self.out))
        self.enterContext(contextlib.redirect_stderr(self.err))
        clock = [0.0]
        self.enterContext(patch.object(wb, 'now', lambda: clock[0]))
        self.enterContext(patch.object(wb, 'pause', lambda seconds: clock.__setitem__(0, clock[0] + seconds)))

    def run_wb(self, *argv, gh):
        with patch.object(sys, 'argv', ['wb.py', *argv]), patch.object(wb.subprocess, 'run', side_effect=gh):
            return wb.main()

    # --- the scoping rule --------------------------------------------------------------------------

    def test_a_call_without_a_repo_names_the_workbench_repo(self):
        self.assertEqual(['issue', 'view', '5', '--json', 'title', '--repo', 'fork/r'],
                         wb.scoped(['issue', 'view', '5', '--json', 'title'], 'fork/r'))
        for verb in (['pr', 'checks', '7'], ['label', 'create', 'x'], ['run', 'rerun', '11', '--failed']):
            with self.subTest(verb=verb):
                self.assertEqual(verb + ['--repo', 'fork/r'], wb.scoped(verb, 'fork/r'))

    def test_a_named_repo_is_left_alone(self):
        for args in (['issue', 'view', '5', '--repo', 'up/r'], ['issue', 'view', '5', '-R', 'up/r'],
                     ['issue', 'view', '5', '--repo=up/r'], ['issue', 'view', '5', '-Rup/r'],
                     ['issue', 'comment', '5', '--repo', 'github.com/Fork/R', '--body', 'x']):
            with self.subTest(args=args):
                self.assertEqual(args, wb.scoped(args, 'fork/r'))          # reads anywhere; writes here, any case

    def test_a_url_argument_still_gets_the_repo(self):
        # r1 M1: gh takes the URL's repo over --repo; the append means a misread can never drop it.
        for args in (['pr', 'view', 'https://github.com/up/r/pull/7'], ['pr', 'merge', 'https://github.com/FORK/r/pull/7']):
            with self.subTest(args=args):
                self.assertEqual(args + ['--repo', 'fork/r'], wb.scoped(args, 'fork/r'))

    def test_flag_values_are_never_read_as_flags_or_arguments(self):
        # r1 M1: a title like `-Recurse ...` read as `-R ecurse ...` made filing fail forever; a title that
        # was a URL dropped the --repo and left the repo to gh's base - the #71 bug itself.
        for args in (['issue', 'create', '--title', '-Recurse is dropped', '--body-file', 'x'],
                     ['issue', 'create', '--title', '-R', '--body-file', 'x'],
                     ['issue', 'create', '--title', 'https://github.com/fork/r/issues/3', '--body-file', 'x'],
                     ['issue', 'create', '--title=-Rx'], ['issue', 'create', '-t', '-R'],
                     ['issue', 'create', '--body', '--repo up/r'], ['issue', 'create', '--body', '--repo=up/r'],
                     ['issue', 'list', '--search', '"-R up/r" in:title', '--json', 'title'],
                     ['issue', 'edit', '5', '--add-label', '-Rup/r']):
            with self.subTest(args=args):
                self.assertEqual(args + ['--repo', 'fork/r'], wb.scoped(args, 'fork/r'))
        self.assertEqual(['issue', 'create', '--title', 't', '--repo', 'fork/r', '--', '-R'],
                         wb.scoped(['issue', 'create', '--title', 't', '--', '-R'], 'fork/r'))
        # r2 i1: a `--` that is a flag's value ends nothing.
        self.assertEqual(['issue', 'create', '--title', '--', '--body-file', 'x', '--repo', 'fork/r'],
                         wb.scoped(['issue', 'create', '--title', '--', '--body-file', 'x'], 'fork/r'))

    def test_a_switch_before_a_foreign_url_cannot_hide_it(self):
        # r2 m1: `-s` is a switch on `pr merge` but a value flag elsewhere; a write refuses on any token.
        for args in (['pr', 'merge', '-s', 'https://github.com/up/r/pull/1'],
                     ['pr', 'review', '-a', 'https://github.com/up/r/pull/1'],
                     ['pr', 'close', '-d', 'https://github.com/up/r/pull/1'],
                     ['issue', 'close', '-r', 'https://github.com/up/r/issues/1']):
            with self.subTest(args=args), self.assertRaises(wb.ForeignRepo) as caught:
                wb.scoped(args, 'fork/r')
            self.assertIn('targets up/r', str(caught.exception))
        # The price of failing closed: a write whose text is exactly another repo's URL is refused too.
        with self.assertRaises(wb.ForeignRepo):
            wb.scoped(['issue', 'create', '--title', 'https://github.com/up/r/issues/3', '--body-file', 'x'], 'fork/r')

    def test_a_write_to_another_repo_is_refused(self):
        for args in (['issue', 'create', '--title', 't', '--repo', 'up/r'],
                     ['issue', 'comment', 'https://github.com/up/r/issues/1', '--body-file', 'b'],
                     ['issue', 'edit', '1', '-R', 'github.com/up/r'], ['label', 'create', 'x', '--repo=up/r'],
                     ['pr', 'merge', 'https://github.com/up/r/pull/1'], ['pr', 'comment', '1', '-Rup/r'],
                     ['run', 'rerun', 'https://github.com/up/r/actions/runs/11']):
            with self.subTest(args=args), self.assertRaises(wb.ForeignRepo) as caught:
                wb.scoped(args, 'fork/r')
            self.assertIn('up/r', str(caught.exception))
            self.assertIn('fork/r', str(caught.exception))

    def test_api_writes_to_another_repo_are_refused(self):
        for args in (['api', '-X', 'PATCH', 'repos/up/r/issues/1'], ['api', 'repos/up/r/issues/1/comments', '-f', 'body=x'],
                     ['api', '--method=POST', '/repos/up/r/issues'], ['api', '-XDELETE', 'repos/up/r/labels/x'],
                     ['api', 'repos/up/r/issues', '-F', 'title=t'], ['api', 'repos/up/r/issues', '--input', 'body.json'],
                     ['api', '--method', 'PUT', 'repos/UP/r/pulls/1/merge']):
            with self.subTest(args=args), self.assertRaises(wb.ForeignRepo) as caught:
                wb.scoped(args, 'fork/r')
            self.assertIn('fork/r', str(caught.exception))

    def test_api_reads_and_own_writes_pass(self):
        for args in (['api', 'repos/up/r/issues/1'], ['api', '--paginate', 'repos/up/r/issues?state=all'],
                     ['api', '-X', 'GET', 'repos/up/r/issues', '-f', 'state=open'],
                     ['api', '-X', 'PATCH', 'repos/Fork/R/issues/1', '-f', 'body=x'],
                     ['api', 'graphql', '-f', 'query=x']):
            with self.subTest(args=args):
                self.assertEqual(args, wb.scoped(args, 'fork/r'))

    def test_api_placeholders_are_the_workbench_repo(self):
        # gh fills {owner}/{repo} from its own base resolution - the parent in a fork's checkout.
        self.assertEqual(['api', 'repos/fork/r/issues', '-F', 'owner=fork', '-f', 'body={repo}'],
                         wb.scoped(['api', 'repos/{owner}/{repo}/issues', '-F', 'owner={owner}', '-f', 'body={repo}'],
                                   'fork/r'))

    def test_a_refused_call_never_reaches_gh(self):
        gh = ForkGh()
        with patch.object(wb.subprocess, 'run', side_effect=gh):
            done = wb.gh_run(self.folder, 'issue', 'create', '--title', 't', '--repo', 'up/r')
        self.assertEqual(1, done.returncode)
        self.assertIn("targets up/r, not this workbench's repo fork/r", done.stderr)
        self.assertIn('wb: refused: gh issue create targets up/r', self.err.getvalue())
        self.assertEqual([], gh.calls)
        with patch.object(wb.subprocess, 'run', side_effect=gh), self.assertRaises(wb.ForeignRepo):
            wb.gh_json('api', '-X', 'PATCH', 'repos/up/r/issues/1', cwd=self.folder)
        self.assertEqual([], gh.calls)

    # --- the workbench repo ------------------------------------------------------------------------

    def test_the_repo_is_the_checkouts_origin_with_its_case(self):
        for url in ('https://github.com/Fork/Re.po.git', 'https://x-token@github.com/Fork/Re.po', 'git@github.com:Fork/Re.po.git',
                    'ssh://git@github.com/Fork/Re.po.git/'):
            with self.subTest(url=url):
                folder = scratch(self, 'wb-origin-')
                make_origin(folder)
                subprocess.run(['git', '-C', str(folder), 'remote', 'set-url', 'origin', url], check=True)
                self.assertEqual('Fork/Re.po', wb.workbench_repo(folder))

    def test_no_github_origin_is_exit_2_not_a_guess(self):
        folder = scratch(self, 'wb-origin-')
        make_origin(folder)
        subprocess.run(['git', '-C', str(folder), 'remote', 'set-url', 'origin', 'https://gitlab.com/o/r.git'], check=True)
        with self.assertRaises(SystemExit) as caught:
            wb.workbench_repo(folder)
        self.assertEqual(2, caught.exception.code)
        self.assertIn('has no GitHub origin', self.err.getvalue())
        # A folder inside another clone is not a checkout: git must not walk up to that clone's origin.
        inside = Path(__file__).resolve().parent.parent / ('test wb fork ' + uuid.uuid4().hex)
        inside.mkdir()
        self.addCleanup(shutil.rmtree, inside)
        with self.assertRaises(SystemExit) as caught:
            wb.workbench_repo(inside)
        self.assertEqual(2, caught.exception.code)

    # --- the commands (AC3) ------------------------------------------------------------------------

    def test_merge_check_wait_ci_ci_log_ci_rerun_and_no_pr_done_name_the_fork(self):
        for argv in (['merge-check', '--pr', '7', '--head', HEAD], ['wait-ci', '--pr', '7', '--head', HEAD],
                     ['ci-log', '--pr', '7'], ['ci-rerun', '--pr', '7'],
                     ['loop-state', 'done', '--no-pr', '--reason', 'duplicate of #2']):
            with self.subTest(command=argv[0]):
                gh = ForkGh()
                self.run_wb(*argv, gh=gh)
                self.assertTrue(gh.calls)
                self.assertEqual([], gh.unscoped)
        verbs = {tuple(c[1:3]) for c in gh.calls}
        self.assertEqual({('issue', 'view'), ('pr', 'list')}, verbs)

    def test_every_gh_command_the_planner_is_told_to_type_names_the_repo(self):
        text = (Path(__file__).resolve().parent.parent / 'claude/commands/start-github-issue.md').read_text(encoding='utf-8')
        # A command with arguments, not a bare mention like `gh pr create` or the `gh issue ...` of the rule.
        commands = [m[0] for m in re.finditer(r'gh (?:issue|pr|repo|label|run) [a-z-]+ [^`\n]+', text)
                    if not m[0].rstrip().endswith('...')]
        self.assertGreaterEqual(len(commands), 7)                            # view, close, create, edit, merge, comment...
        for command in commands:
            with self.subTest(command=command):
                self.assertTrue('--repo <owner/repo>' in command or command.startswith('gh repo view <owner/repo>'),
                                command)
        self.assertIn("In a fork's clone gh's own default is the fork's **parent**", text)

    def test_merge_check_refuses_a_pr_of_another_repo(self):
        gh = ForkGh(pr_repo='up/r')
        self.assertEqual(1, self.run_wb('merge-check', '--pr', 'https://github.com/up/r/pull/7', '--head', HEAD, gh=gh))
        self.assertEqual("repo: PR https://github.com/up/r/pull/7 is in up/r, not this workbench's repo fork/r",
                         self.out.getvalue().strip())
        self.assertEqual([['pr', 'view']], [c[1:3] for c in gh.calls])       # nothing more is read of it


class WaitLimit(unittest.TestCase):
    """#88: in a -WaitOnLimit loop a usage limit is waited out by the relay, never reported blocked."""

    def setUp(self):
        import conductor
        self.q = conductor
        self.folder = Path(__file__).resolve().parent.parent / ('test wb wait-limit ' + uuid.uuid4().hex)
        self.folder.mkdir()
        self.addCleanup(shutil.rmtree, self.folder)
        self.state = self.folder / '.workbench/state'
        self.state.mkdir(parents=True)
        self.loop = str(uuid.uuid4())
        self.q.atomic_json(self.state / 'queue-member.json', dict(queue=str(self.folder / 'queue.json'), repo='o/r', number=1))
        self.q.atomic_json(self.state / 'claude.json', dict(sessionId=self.loop))
        self.enterContext(patch.dict(os.environ, AI_HUB=str(self.folder / '.workbench'), CLAUDE_CODE_SESSION_ID=self.loop))
        self.enterContext(patch.object(agw, 'request', side_effect=AssertionError('terminal access')))
        self.err = self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        self.out = self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def on_limit(self, value):
        self.q.atomic_json(self.state / 'implementer.json', {'tool': 'kimi', 'onLimit': value})

    def run_wb(self, *argv):
        with patch.object(sys, 'argv', ['wb.py', *argv]):
            return wb.main()

    def test_wait_limit_files_the_request_for_the_relay(self):
        self.on_limit('wait')
        self.assertEqual(0, self.run_wb('wait-limit', '--reason', 'kimi 5-hour limit, the relay missed it'))
        request = self.q.read_json(self.state / 'limit-request.json')
        self.assertEqual(('codex', 'kimi 5-hour limit, the relay missed it'), (request['box'], request['reason']))
        self.assertIsInstance(request['at'], (int, float))
        self.assertEqual(0, self.run_wb('wait-limit', '--box', 'claude', '--reason', 'my own limit'))
        self.assertEqual('claude', self.q.read_json(self.state / 'limit-request.json')['box'])

    def test_wait_limit_outside_wait_mode_says_fail_over(self):
        for value in ('failover', None):
            with self.subTest(onLimit=value):
                if value:
                    self.on_limit(value)
                else:
                    (self.state / 'implementer.json').unlink()      # no settings record: failover
                self.err.seek(0)
                self.err.truncate()
                self.assertEqual(1, self.run_wb('wait-limit', '--reason', 'kimi limited'))
                self.assertFalse((self.state / 'limit-request.json').exists())
                self.assertIn('-Failover', self.err.getvalue())

    # Rounds 1-2: a limit an agent waits out, in every wording the reviewers tried, and the agents' own.
    AGENT_LIMITS = (
        'implementer (kimi) at its usage limit', 'Kimi quota exhausted', "kimi's quota is used up", 'plan quota reached',
        'waiting out the 5-HOUR window', 'codex limited', 'Kimi hit its limit', 'claude has reached the weekly limit',
        'Kimi implementer limited', 'The Kimi implementer hit its limit', 'kimi (implementer) hit its limit',
        'implementer (kimi): limited', 'the implementer (Kimi Code) hit its limit', 'kimi 5h limit', 'Kimi 5 hour limit',
        'kimi hit 5h limit', 'Kimi: 5 hour limit', 'kimi: 5h limit hit', 'kimi out of quota', 'kimi ran out of quota',
        'kimi exhausted its quota', 'kimi weekly quota', 'kimi: quota exceeded', 'Kimi: 403 quota exhausted',
        'Kimi rate limited', 'codex hit rate limit', 'codex hit its rate limit', 'Kimi plan limit reached',
        'kimi is over its limit', 'codex exceeded its limit', 'codex: limited', 'kimi: limit reached', 'kimi-code limited',
        'implementer usage-limited', "claude: You've hit your limit", "claude: You’ve hit your session limit",
        'codex out of credits', 'Kimi: Error: [provider.auth_error] 403 exceeded your current quota',
        # FIX r3: the window in hours, Kimi's own wording with no limit word, and (no bare keyword) Claude's
        # own message, which only limits.LIMITED and the apostrophe fold catch.
        'kimi exhausted; resets in 5 hours', 'Kimi: insufficient balance; please recharge your account',
        'kimi: Error: [provider.billing] insufficient balance', 'claude: You’ve hit your budget')
    # Rounds 1-2: real blocks that mention a limit - the human answers them, with --needs-human.
    REAL_BLOCKS = (
        'GitHub API rate limit', 'CI runner limited', 'disk quota exceeded', 'GitHub Actions minutes quota exhausted',
        'CI runner quota reached', 'codex push failed: GitHub API rate limit', 'claude cannot push: GitHub API rate limit',
        'claude needs a human: PR body exceeds the 65536 char limit', 'kimi: CI time limit exceeded',
        'codex: CI runner time limit', 'codex sandbox: network limited', 'CLAUDE.md line limit question',
        'codex limited; failover is off', 'kimi limited; failover is off',
        'codex is limited by the sandbox: cannot write outside the repo',
        'claude has limited context left, needs a new session', 'Claude limits the PR to 500 lines? question for human',
        'kimi reached the limit of 3 review rounds', 'kimi hit the limit of retries on flaky CI',
        'codex is at the limit of what it can verify without secrets', 'codex has hit the limit on open PRs',
        'kimi is limited to read-only sandbox; needs human', 'GitHub API usage limit exceeded',
        'GitHub Actions usage limit reached', 'GitHub Actions usage quota exhausted', 'Copilot usage limit',
        'Azure OpenAI usage limit on the CI key', 'claude needs a human: plan quota for GitHub Copilot',
        "failover refused: codex recorded limited at 10:02 ('You've hit your usage limit')")

    def blocked(self, reason, *flags):
        self.err.seek(0)
        self.err.truncate()
        return self.run_wb('loop-state', 'blocked', *flags, '--reason', reason)

    def test_any_limit_reason_is_refused_in_wait_mode_until_the_planner_answers(self):
        # FIX r2: a free-text pattern cannot tell a usage limit from a GitHub one; the planner says which.
        self.on_limit('wait')
        for reason in self.AGENT_LIMITS + self.REAL_BLOCKS:
            with self.subTest(reason=reason):
                self.assertEqual(1, self.blocked(reason))
                stderr = self.err.getvalue()
                self.assertIn('wb.py wait-limit', stderr)
                self.assertIn('--needs-human', stderr)
                self.assertLess(stderr.index('wait-limit'), stderr.index('--needs-human'))
                self.assertEqual(1, self.blocked(reason, '--environmental'))
        self.assertFalse((self.state / 'loop.json').exists())
        for reason in self.REAL_BLOCKS:
            with self.subTest(needs_human=reason):
                self.assertEqual(0, self.blocked(reason, '--environmental', '--needs-human'))
                self.assertEqual(reason, self.q.read_json(self.state / 'loop.json')['reason'])
        self.assertEqual(0, self.blocked('codex limited; failover is off', '--needs-human'))

    def test_a_reason_with_no_limit_word_needs_no_answer(self):
        self.on_limit('wait')
        for reason in ('plan disagreement', 'review rounds exhausted: the parser still drops rows', 'PR closed',
                       'mail waiter configuration error', 'a question for the human',
                       'no-op: already fixed in #12; close the issue to finish'):
            with self.subTest(reason=reason):
                self.assertEqual(0, self.blocked(reason))

    def test_blocked_for_a_usage_limit_is_allowed_when_failing_over(self):
        self.on_limit('failover')
        for reason in ('implementer (kimi) at its usage limit', 'codex limited; failover is off'):
            with self.subTest(reason=reason):
                self.assertEqual(0, self.blocked(reason, '--environmental'))
                self.assertEqual('blocked', self.q.read_json(self.state / 'loop.json')['state'])

    def test_the_planner_doc_answers_the_refusal_where_it_prescribes_a_limit_block(self):
        doc = (Path(__file__).resolve().parent.parent / 'claude/commands/start-github-issue.md').read_text(encoding='utf-8')
        usage = doc.split('## Usage limits')[1].split('## Stall pointers')[0]
        for command in ('loop-state blocked --environmental --needs-human --reason "<the refusal line>"',
                        'loop-state blocked --environmental --needs-human --reason "<tool> limited; failover is off"',
                        '`--environmental --needs-human`', 'wait-limit --reason'):
            with self.subTest(command=command):
                self.assertIn(command, usage)
        # FIX r3 M1: where the doc names an environmental block, an agent's usage limit still goes to wait-limit.
        environmental = ' '.join(doc.split('is **environmental**')[1].split('A question for the human')[0].split())
        self.assertIn('wb.py wait-limit', environmental)
        self.assertLess(environmental.index('wait-limit'), environmental.index('--needs-human'))
        # Every prescribed reason that mentions a limit carries the answer.
        for command, reason in re.findall(r'(loop-state blocked[^`"]*)--reason "([^"]*)"', doc):
            if wb.limit_reason(reason):
                with self.subTest(reason=reason):
                    self.assertIn('--needs-human', command)

    def test_needs_human_is_only_for_blocked(self):
        self.on_limit('wait')
        with patch.object(sys, 'argv', ['wb.py', 'loop-state', 'resumed', '--environmental']):
            environmental = wb.main()
        for state in ('resumed', 'pr-open'):
            with self.subTest(state=state):
                argv = ['loop-state', state, '--needs-human'] + (['--pr', 'https://github.com/o/r/pull/2']
                                                                 if state == 'pr-open' else [])
                self.assertEqual(environmental, self.run_wb(*argv))
        self.assertEqual(2, environmental)
        self.assertFalse((self.state / 'loop.json').exists())


if __name__ == '__main__':
    unittest.main()
