"""#45: helpers that finish where agents can see them - a UTF-8 log, a completion marker, a mail."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'lib'))

import agw  # noqa: E402
import closer  # noqa: E402
import hub  # noqa: E402
import run_helper  # noqa: E402
import wb  # noqa: E402

FAILING = ("import sys; print('héllo ✓ 日本'); print('Ran 5 tests'); print(); "
           "print('FAILED (failures=2, errors=1)'); sys.exit(1)")
UTF16 = ("import sys; sys.stdout.buffer.write('SUITE FAILURES: 0 — ok\\r\\n'.encode('utf-16')); "
         "sys.stdout.flush()")


class Wrapper(unittest.TestCase):
    def setUp(self):
        self.folder = Path(__file__).resolve().parent.parent / ('test run helper ' + uuid.uuid4().hex)
        self.hub_dir = self.folder / '.workbench'
        self.hub_dir.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.addCleanup(hub.reload_paths)
        self.enterContext(patch.dict(os.environ, {'AGWINTERM_PANE_ID': 'helper-1', 'AI_HUB': str(self.hub_dir)}))
        self.screen = io.StringIO()
        # The pane shows what the wrapper printed: the marker's rows are read from it.
        self.enterContext(patch.object(agw, 'pane_text', side_effect=lambda pane: self.screen.getvalue()))
        self.enterContext(patch.object(agw, 'request', side_effect=AssertionError('real terminal request')))

    def run_wrapper(self, *command, label='abc1234', to=None):
        argv = ['--hub', str(self.hub_dir), '--label', label, *(['--to', to] if to else []), '--', *command]
        with contextlib.redirect_stdout(self.screen):
            return run_helper.main(argv)

    def marker(self):
        return json.loads((self.hub_dir / 'state' / 'helpers' / 'helper-1.done').read_text(encoding='utf-8'))

    def mails(self, box='claude'):
        return [hub.parse_message(path) for path in sorted((self.hub_dir / 'inbox' / box).glob('*.md'))]

    def test_a_failing_suite_logs_utf8_marks_done_and_mails_the_planner(self):
        code = self.run_wrapper(sys.executable, '-c', FAILING)
        self.assertEqual(1, code)
        log = self.hub_dir / 'review' / 'suite-abc1234.log'
        raw = log.read_bytes()
        self.assertFalse(raw.startswith(b'\xef\xbb\xbf'), 'no BOM')
        self.assertIn('héllo ✓ 日本', raw.decode('utf-8'))
        grep = shutil.which('grep')
        if grep:
            found = subprocess.run([grep, '-c', 'FAILED (failures=2', str(log)], capture_output=True, text=True)
            self.assertEqual('1', found.stdout.strip(), found.stderr)
        marker = self.marker()
        self.assertEqual(('suite', 1, 3, 'helper-1'), (marker['kind'], marker['exit'], marker['failures'], marker['pane']))
        [mail] = self.mails()
        self.assertEqual(('helper', 'note', 'suite abc1234: FAILED (exit 1, 3 failures)'),
                         (mail['from'], mail['kind'], mail['subject']))
        self.assertIn('FAILED (failures=2, errors=1)', mail['body'])
        self.assertIn(str(log), mail['body'])

    @unittest.skipUnless(shutil.which('grep'), 'no grep on PATH')
    def test_plain_grep_reads_the_log(self):
        self.run_wrapper(sys.executable, '-c', FAILING)
        found = subprocess.run(['grep', '-c', 'héllo', str(self.hub_dir / 'review' / 'suite-abc1234.log')],
                               capture_output=True, env=dict(os.environ, LC_ALL='C.UTF-8'))
        self.assertEqual(b'1', found.stdout.strip(), found.stderr)

    def test_the_marker_is_the_last_act(self):
        # G3: the marker's rows are exactly the pane's last rows, the mail line included - nothing is
        # printed after it, so the autonomous close can prove the pane untouched.
        self.run_wrapper(sys.executable, '-c', 'print("ok")')
        rows = self.marker()['rows']
        self.assertEqual(closer.filled_rows(self.screen.getvalue()), rows)
        self.assertTrue(rows[-1].startswith('result mailed to claude ('), rows[-1])

    def test_a_utf16_child_is_logged_as_utf8(self):
        self.assertEqual(0, self.run_wrapper(sys.executable, '-c', UTF16))
        text = (self.hub_dir / 'review' / 'suite-abc1234.log').read_bytes().decode('utf-8')
        self.assertIn('SUITE FAILURES: 0 — ok', text)
        self.assertEqual(0, self.marker()['failures'])
        self.assertEqual('suite abc1234: passed (exit 0, 0 failures)', self.mails()[0]['subject'])

    def test_the_result_goes_to_the_named_box(self):
        self.run_wrapper(sys.executable, '-c', 'print("OK")', to='codex')
        self.assertEqual([], self.mails('claude'))
        self.assertEqual('suite abc1234: passed (exit 0, 0 failures)', self.mails('codex')[0]['subject'])

    def test_a_command_that_cannot_start_still_mails_and_marks(self):
        self.assertEqual(1, self.run_wrapper(str(self.folder / 'no such program.exe')))
        self.assertIsNone(self.marker()['exit'])
        mail = self.mails()[0]
        self.assertEqual('suite abc1234: FAILED (did not finish)', mail['subject'])
        self.assertIn('could not start', mail['body'])

    def test_arguments_reach_the_child_unchanged(self):
        # G4: argv, no shell - spaces and quotes survive.
        self.run_wrapper(sys.executable, '-c', 'import sys, json; print(json.dumps(sys.argv[1:]))',
                         'a b', 'it\'s "quoted"', '$HOME')
        log = (self.hub_dir / 'review' / 'suite-abc1234.log').read_text(encoding='utf-8')
        self.assertEqual(['a b', 'it\'s "quoted"', '$HOME'], json.loads(log.splitlines()[0]))


class Pieces(unittest.TestCase):
    def test_failure_counts(self):
        for text, expected in (('SUITE FAILURES: 0', 0), ('SUITE FAILURES: 4\n', 4),
                               ('Ran 3 tests\n\nFAILED (failures=2, errors=1)', 3), ('FAILED (errors=2)', 2),
                               ('Ran 725 tests in 938.825s\n\nOK', 0), ('OK (skipped=3)', 0),
                               ('==== 3 failed, 10 passed in 2.1s ====', 3), ('== 1 failed, 2 errors in 1s ==', 3),
                               ('test result: FAILED. 8 passed; 2 failed; 0 ignored', 2),
                               ('nothing here', None), ('', None)):
            with self.subTest(text=text):
                self.assertEqual(expected, run_helper.count_failures(text))

    def test_a_ps1_runs_under_powershell(self):
        command = run_helper.command_for(['build.ps1', '-Release'])
        self.assertEqual(['-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', 'build.ps1', '-Release'],
                         command[1:])
        self.assertRegex(Path(command[0]).name.lower(), r'^(pwsh|powershell)(\.exe)?$')
        self.assertEqual(['python', '-m', 'unittest'], run_helper.command_for(['python', '-m', 'unittest']))

    def test_decoder_choice(self):
        self.assertEqual('é', run_helper.pick_decoder('é'.encode('utf-8')).decode('é'.encode('utf-8')))
        text = 'Ran 5 tests'
        self.assertEqual(text, run_helper.pick_decoder(text.encode('utf-16-le')).decode(text.encode('utf-16-le')))
        self.assertEqual(text, run_helper.pick_decoder(text.encode('utf-16')).decode(text.encode('utf-16')))
        self.assertEqual('x�', run_helper.pick_decoder(b'x\xff').decode(b'x\xff'))

    def test_labels_are_checked(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            run_helper.main(['--hub', 'h', '--label', 'a b', '--', 'python'])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            run_helper.main(['--hub', 'h', '--label', 'ok'])


class SuiteCommand(unittest.TestCase):
    """`wb.py suite` opens the visible direct-mode session that runs the wrapper."""

    def setUp(self):
        self.folder = Path(__file__).resolve().parent.parent / ('test wb suite ' + uuid.uuid4().hex)
        (self.folder / '.workbench/state').mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.enterContext(patch.dict(os.environ, {'AI_HUB': str(self.folder / '.workbench')}))
        os.environ.pop('AI_BOX', None)
        self.opened = self.enterContext(patch.object(wb, 'open_session', return_value='sid'))
        self.enterContext(patch.object(wb, 'issue_number', return_value='45'))
        self.out = self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def suite(self, *argv):
        with patch.object(sys, 'argv', ['wb.py', 'suite', *argv]):
            return wb.main()

    @unittest.skipUnless(sys.platform == 'win32', 'Windows quoting')
    def test_the_session_and_its_command_line(self):
        self.assertEqual(0, self.suite('--label', '1f04542', '--', 'python', '-m', 'unittest', 'discover', '-s', 'my tests'))
        name, cwd, line = self.opened.call_args.args
        self.assertEqual(('#45 suite 1f04542', self.folder.resolve(), False),
                         (name, cwd, self.opened.call_args.kwargs['select']))
        import ctypes
        from ctypes import wintypes
        parse = ctypes.windll.shell32.CommandLineToArgvW
        parse.argtypes, parse.restype = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)], ctypes.POINTER(wintypes.LPWSTR)
        count = ctypes.c_int()
        argv = parse(line, ctypes.byref(count))
        parsed = [argv[i] for i in range(count.value)]
        ctypes.windll.kernel32.LocalFree(argv)
        self.assertEqual(str(wb.HERE / 'run_helper.py'), parsed[1])
        self.assertEqual(['--hub', str(self.folder.resolve() / '.workbench'), '--label', '1f04542', '--to', 'claude',
                          '--', 'python', '-m', 'unittest', 'discover', '-s', 'my tests'], parsed[2:])
        self.assertIn("mail from 'helper' to claude", self.out.getvalue())

    def test_the_result_goes_to_the_callers_box(self):
        with patch.dict(os.environ, {'AI_BOX': 'codex'}):
            self.suite('--label', 'x', '--', 'python')
        self.assertIn('--to codex', self.opened.call_args.args[2])
        self.suite('--label', 'x', '--to', 'claude', '--', 'python')
        self.assertIn('--to claude', self.opened.call_args.args[2])

    def test_bad_labels_and_missing_commands_are_refused(self):
        for argv in (('--label', 'a;b', '--', 'python'), ('--label', 'ok'), ('--label', 'ok', '--')):
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                self.suite(*argv)
        self.opened.assert_not_called()


class WaitingRecord(unittest.TestCase):
    """B1: `wb.py status blocked` leaves a durable record; the sidebar status cannot be one."""

    def setUp(self):
        self.folder = Path(__file__).resolve().parent.parent / ('test wb waiting ' + uuid.uuid4().hex)
        (self.folder / '.workbench/state').mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.enterContext(patch.dict(os.environ, {'AI_HUB': str(self.folder / '.workbench')}))
        self.set_status = self.enterContext(patch.object(agw, 'set_status'))
        self.path = self.folder / '.workbench/state/waiting.json'

    def wb(self, *argv):
        with patch.object(sys, 'argv', ['wb.py', *argv]), contextlib.redirect_stdout(io.StringIO()):
            return wb.main()

    def test_blocked_writes_it_and_any_other_status_removes_it(self):
        self.wb('status', 'blocked', '--sound')
        self.assertEqual('planner', json.loads(self.path.read_text(encoding='utf-8'))['by'])
        self.set_status.assert_called_with('blocked', sound=True, blink=True)
        for state in ('active', 'idle', 'completed'):
            with self.subTest(state=state):
                self.wb('status', 'blocked')
                self.wb('status', state)
                self.assertFalse(self.path.exists())

    def test_loop_state_done_and_resumed_remove_it(self):
        self.wb('status', 'blocked')
        self.assertEqual(0, self.wb('loop-state', 'done', '--pr', '7', '--sha', 'abc'))
        self.assertFalse(self.path.exists())
        self.wb('status', 'blocked')
        with patch('conductor.write_loop_state', return_value={}):
            self.assertEqual(0, self.wb('loop-state', 'resumed'))
        self.assertFalse(self.path.exists())

    def test_it_is_written_even_when_the_terminal_is_unreachable(self):
        self.set_status.side_effect = agw.CtlError('no pipe')
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(1, self.wb('status', 'blocked'))
        self.assertTrue(self.path.exists())


if __name__ == '__main__':
    unittest.main()
