"""#45: the relay's stall watch, with a fake clock, a stub terminal and a temp mailbox.

A loop that sits idle with nothing to wake it gets one `stall` pointer after stallMinutes, and is
reported blocked after two more periods with no progress. Nothing that means "the loop is waiting
correctly" may raise one.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import agw  # noqa: E402
import closer  # noqa: E402
import hub  # noqa: E402
import peerchat  # noqa: E402
import relay  # noqa: E402
from frames import CLAUDE_IDLE, CLAUDE_RUNNING, CODEX_IDLE, claude, codex  # noqa: E402

PLANNER, IMPLEMENTER, HELPER = 'planner-pane', 'implementer-pane', 'helper-pane'
MIN = 60.0
S = 15          # stallMinutes in these tests


class StallFixture(unittest.TestCase):
    def setUp(self):
        self.folder = Path(__file__).resolve().parent.parent / ('test stall ' + uuid.uuid4().hex)
        self.hub_dir = self.folder / '.workbench'
        (self.hub_dir / 'state').mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.addCleanup(hub.reload_paths)
        config = self.folder / 'config.json'
        config.write_text('{}', encoding='utf-8')
        # The human's real ~/.agworkbench.json never reaches a result here.
        self.enterContext(patch.dict(os.environ, {'AGWORKBENCH_CONFIG': str(config)}))
        self.t = 0.0
        self.enterContext(patch.object(relay, 'now', lambda: self.t))
        self.enterContext(patch.object(relay, 'wall', lambda: 1_000_000 + self.t))
        self.head = 'a' * 40
        self.enterContext(patch.object(relay, 'git_head', lambda root: self.head))
        self.text = {PLANNER: CLAUDE_IDLE, IMPLEMENTER: CODEX_IDLE, HELPER: 'running tests ...'}
        self.enterContext(patch.object(agw, 'pane_text', side_effect=self.pane_text))
        self.sessions = [{'id': 'issue', 'name': '#7 fix', 'paneIds': [PLANNER, IMPLEMENTER]}]
        self.enterContext(patch.object(agw, 'tree', side_effect=lambda: {
            'workspaces': [{'name': 'repo', 'sessions': self.sessions}]}))
        self.status = self.enterContext(patch.object(agw, 'set_status'))
        self.notify = self.enterContext(patch.object(agw, 'notify'))
        self.enterContext(patch.object(agw, 'request', side_effect=AssertionError('real terminal request')))
        self.r = self.make_relay()

    def make_relay(self, minutes=S, dry_run=False):
        peers = [relay.Peer('claude', 'claude', PLANNER), relay.Peer('codex', 'codex', IMPLEMENTER)]
        made = relay.Relay(self.hub_dir, peers, 'o/repo', 'issue-7-fix', 5, 60, dry_run=dry_run,
                           stall_minutes=minutes)
        self.logs = []
        made.log = self.logs.append
        return made

    def pane_text(self, pane):
        value = self.text[pane]
        if isinstance(value, Exception):
            raise value
        return value

    def tick(self, minute):
        self.t = minute * MIN
        self.r.stall.tick(self.r.read_panes())

    def run_until(self, last, start=0, step=1):
        minute = start
        while minute <= last:
            self.tick(minute)
            minute += step

    def stall_mail(self):
        box = self.hub_dir / 'inbox' / 'claude'
        found = []
        for path in sorted([*box.glob('*.md'), *(box / 'read').glob('*.md')]):
            message = hub.parse_message(path)
            if message.get('from') == 'relay' and message.get('kind') == 'stall':
                found.append(message)
        return found

    def escalations(self):
        return [c for c in self.status.call_args_list if c.args[:1] == ('blocked',)]

    def write(self, name, data):
        (self.hub_dir / 'state' / name).write_text(json.dumps(data), encoding='utf-8')

    def mail(self, box, sender='claude', folder=''):
        path = hub.write_message(to=box, sender=sender, subject='x', body='y', kind='message')
        if folder:
            hub.mark_read(path)
        return path


class Pointer(StallFixture):
    def test_one_pointer_after_the_stall_period_and_no_second_before_escalation(self):
        self.run_until(S - 1)
        self.assertEqual([], self.stall_mail())
        self.tick(S)
        mails = self.stall_mail()
        self.assertEqual(1, len(mails))
        self.assertEqual(('claude', 'relay', 'stall'), (mails[0]['to'], mails[0]['from'], mails[0]['kind']))
        self.assertTrue(mails[0]['subject'].startswith('stall:'), mails[0]['subject'])
        self.assertIn('mail waiter', mails[0]['body'])
        self.run_until(3 * S - 1, start=S + 1)
        self.assertEqual(1, len(self.stall_mail()))
        self.assertEqual([], self.escalations())

    def test_the_pointer_names_the_implementers_last_line(self):
        self.run_until(S)
        body = self.stall_mail()[0]['body']
        self.assertIn("The implementer's last line: • No unread mail. Waiting for the next “Chat from Workbench:” "
                      "notification; no files edited.", body)

    def test_the_relay_rings_the_planner_with_the_pointer_without_any_waiter(self):
        # Criterion 7: a planner whose waiter was killed is still woken - the relay rings mail itself.
        self.run_until(S)
        send = self.enterContext(patch.object(peerchat, 'send', return_value='submitted'))
        self.r.deliver_mail()
        send.assert_called_once()
        self.assertEqual(PLANNER, send.call_args.args[0])
        self.assertIn('stall: loop idle for 15 min', send.call_args.args[2])

    def test_the_setting_comes_from_the_config_or_the_flag(self):
        path = Path(os.environ['AGWORKBENCH_CONFIG'])
        for text, expected in (('{}', 15.0), ('{"stallMinutes": 0}', 0.0), ('{"stallMinutes": 7.5}', 7.5),
                               ('{"stallMinutes": -1}', 15.0), ('{"stallMinutes": "5"}', 15.0),
                               ('{"stallMinutes": true}', 15.0), ('not json', 15.0)):
            with self.subTest(text=text):
                path.write_text(text, encoding='utf-8')
                self.assertEqual(expected, relay.stall_setting())
        args = relay.build_parser().parse_args(['--hub', 'h', '--claude-pane', 'c', '--codex-pane', 'x', '--repo', 'o/r',
                                                '--branch', 'b', '--stall-minutes', '3'])
        self.assertEqual(3.0, args.stall_minutes)


class Wiring(StallFixture):
    """r1 F5: main() turns the watch on from the config or the flag, and run() ticks it."""
    PANES = ['11111111-1111-4111-8111-111111111111', '22222222-2222-4222-8222-222222222222']

    def relay_from_main(self, *extra):
        made = []
        # main() stamps both streams (#78): capture them, so no wrapper outlives the test.
        with patch.object(relay.Relay, 'run', autospec=True, side_effect=lambda self: made.append(self) or 0), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(0, relay.main(['--hub', str(self.hub_dir), '--claude-pane', self.PANES[0],
                                            '--codex-pane', self.PANES[1], '--repo', 'o/repo', '--branch', 'issue-7-fix',
                                            *extra]))
        return made[0]

    def test_main_takes_the_period_from_the_config_or_the_flag(self):
        self.assertEqual(15 * 60, self.relay_from_main().stall.period)
        Path(os.environ['AGWORKBENCH_CONFIG']).write_text('{"stallMinutes": 4}', encoding='utf-8')
        self.assertEqual(4 * 60, self.relay_from_main().stall.period)
        self.assertEqual(2 * 60, self.relay_from_main('--stall-minutes', '2').stall.period)
        self.assertEqual(0, self.relay_from_main('--stall-minutes', '0').stall.period)

    def run_loop(self, until):
        """Run the real loop on the fake clock until `until()` holds or an hour passes."""
        self.r.limit_interval = 60
        self.r.mail_interval = 60
        self.r.flush_outbox = lambda: True
        self.r.watch_pr = lambda: False
        self.enterContext(patch.object(peerchat, 'send', return_value='submitted'))

        def pause(seconds):
            self.t += seconds
            if until() or self.t > 3600:
                self.r.stop_file.parent.mkdir(parents=True, exist_ok=True)
                self.r.stop_file.write_text('stop', encoding='utf-8')
        self.enterContext(patch.object(relay, 'pause', pause))
        self.assertEqual(0, self.r.run())

    def test_the_loop_files_the_pointer(self):
        self.run_loop(lambda: bool(self.stall_mail()))
        self.assertEqual(1, len(self.stall_mail()))
        self.assertLessEqual(S * MIN, self.t)
        self.assertLess(self.t, (S + 3) * MIN)

    def test_a_failing_tick_is_logged_and_the_loop_goes_on(self):
        ticks = []

        def broken(texts):
            ticks.append(texts)
            raise RuntimeError('bug')
        self.r.stall.tick = broken
        self.run_loop(lambda: len(ticks) >= 3)
        self.assertEqual(3, len(ticks))
        self.assertIn('stall watch failed: RuntimeError: bug', self.logs)


class Escalation(StallFixture):
    def test_a_pointer_that_could_not_be_filed_is_retried_and_never_cited(self):
        # r1 F6: no level 1 without a pointer on disk.
        real = hub.write_message
        calls = []

        def flaky(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise OSError('disk full')
            return real(**kwargs)
        self.enterContext(patch.object(hub, 'write_message', side_effect=flaky))
        self.run_until(S)
        self.assertEqual([], self.stall_mail())
        self.assertEqual(0, self.r.stall.level)
        self.tick(S + 1)
        self.assertEqual(1, len(self.stall_mail()))
        self.run_until(3 * S, start=S + 2)
        self.assertEqual([], self.escalations())                     # 2S after the real pointer, not the failed one
        self.tick(3 * S + 1)
        self.assertEqual(1, len(self.escalations()))

    def start_queue(self):
        self.loop_id = str(uuid.uuid4())
        self.write('queue-member.json', {'queue': str(self.folder / 'queue.json'), 'repo': 'o/repo', 'number': 7})
        self.write('claude.json', {'sessionId': self.loop_id})

    def test_blocked_after_two_more_periods_exactly_once(self):
        self.start_queue()
        self.run_until(3 * S - 1)
        self.assertEqual([], self.escalations())
        self.tick(3 * S)
        self.assertEqual(1, len(self.escalations()))
        self.status.assert_called_with('blocked', sound=True, blink=True, pane_id=PLANNER)
        self.notify.assert_called_once()
        loop = json.loads((self.hub_dir / 'state' / 'loop.json').read_text(encoding='utf-8'))
        self.assertEqual(('blocked', self.loop_id, 1), (loop['state'], loop['loopId'], loop['rev']))
        self.assertTrue(loop['reason'].startswith('stalled:'), loop['reason'])
        waiting = json.loads((self.hub_dir / 'state' / 'waiting.json').read_text(encoding='utf-8'))
        self.assertEqual('relay', waiting['by'])
        self.run_until(6 * S, start=3 * S + 1)
        self.assertEqual(1, len(self.escalations()))
        self.assertEqual(1, len(self.stall_mail()))

    def test_outside_queue_mode_it_writes_waiting_json_and_no_loop_report(self):
        self.run_until(3 * S)
        self.assertEqual(1, len(self.escalations()))
        self.assertFalse((self.hub_dir / 'state' / 'loop.json').exists())
        self.assertTrue((self.hub_dir / 'state' / 'waiting.json').exists())

    def test_reading_the_pointer_is_not_progress(self):
        self.run_until(S)
        box = self.hub_dir / 'inbox' / 'claude'
        hub.mark_read(next(box.glob('*.md')))
        self.text[PLANNER] = CLAUDE_RUNNING           # the planner reads it, rearms its waiter, ends its turn
        self.tick(S + 1)
        self.text[PLANNER] = CLAUDE_IDLE
        self.run_until(3 * S, start=S + 2)
        self.assertEqual(1, len(self.escalations()))

    def test_never_mid_work_escalation_waits_for_a_whole_idle_period(self):
        # G1: the planner works through the escalation time without a commit or mail.
        self.run_until(S)
        self.text[PLANNER] = CLAUDE_RUNNING
        self.run_until(49, start=S + 1)
        self.assertEqual([], self.escalations())
        self.text[PLANNER] = CLAUDE_IDLE                            # idle from minute 50
        self.run_until(50 + S - 1, start=50)
        self.assertEqual([], self.escalations())
        self.tick(50 + S)
        self.assertEqual(1, len(self.escalations()))
        self.assertEqual(1, len(self.stall_mail()))

    def test_the_planner_can_resume_after_an_escalation(self):
        self.run_until(3 * S)
        (self.hub_dir / 'state' / 'waiting.json').unlink()     # wb.py status active
        self.run_until(3 * S + S - 1, start=3 * S + 1)
        self.assertEqual(1, len(self.stall_mail()))
        self.tick(3 * S + S + 1)
        self.assertEqual(2, len(self.stall_mail()))


class Progress(StallFixture):
    def progress_kinds(self):
        def commit():
            self.head = 'b' * 40

        def mail():
            self.mail('codex', folder='read')              # delivered and read: no longer unread

        def helper():
            self.sessions.append({'id': 'h1', 'name': '#7 suite abc', 'paneIds': [HELPER]})
            (self.hub_dir / 'state' / 'helpers').mkdir(exist_ok=True)
            (self.hub_dir / 'state' / 'helpers' / f'{HELPER}.done').write_text('{}', encoding='utf-8')

        def loop_report():
            self.write('loop.json', {'state': 'resumed', 'rev': 4})
        return {'commit': commit, 'mail': mail, 'helper': helper, 'loop report': loop_report}

    def test_each_kind_of_progress_restarts_the_clock(self):
        for name, make in self.progress_kinds().items():
            with self.subTest(progress=name):
                self.r = self.make_relay()
                for path in (self.hub_dir / 'inbox').rglob('*.md'):
                    path.unlink()
                self.run_until(9)
                make()                                         # seen on the next tick, minute 10
                self.run_until(10 + S - 1, start=10)
                self.assertEqual([], self.stall_mail())
                self.tick(10 + S)
                self.assertEqual(1, len(self.stall_mail()))

    def test_progress_after_the_pointer_cancels_the_escalation(self):
        self.run_until(S)
        self.run_until(29, start=S + 1)
        self.head = 'c' * 40                                        # seen at minute 30
        self.run_until(3 * S, start=30)
        self.assertEqual([], self.escalations())
        self.assertEqual(2, len(self.stall_mail()))                 # a fresh period from minute 30


class ExitedAgent(StallFixture):
    def test_an_exited_agent_is_idle_and_the_pointer_names_it(self):
        # #98 FIX r1 M3: a crashed Kimi left its frame above the prompt, and the exit watch may not restart
        # it (restartExited off): the stall watch still points, and says why the pane is quiet.
        kimi = (Path(__file__).resolve().parent / 'fixtures' / 'kimi' / 'draft-wrapped.txt').read_text(encoding='utf-8')
        self.r.peers[1] = relay.Peer('codex', 'kimi', IMPLEMENTER)
        self.text[IMPLEMENTER] = kimi + '\nPS C:\\repo-issue-7> '
        self.assertEqual([], self.r.stall.busy(self.r.read_panes()))
        self.run_until(S)
        mails = self.stall_mail()
        self.assertEqual(1, len(mails))
        self.assertIn('- codex is at a shell prompt (its agent exited)', mails[0]['body'])

    def assert_quiet(self, minutes=4 * S):
        self.run_until(minutes)
        self.assertEqual([], self.stall_mail())
        self.assertEqual([], self.escalations())

    def test_a_running_helper(self):
        # A helper with no marker whose pane keeps changing.
        self.sessions.append({'id': 'h1', 'name': '#7 revmux r1', 'paneIds': [HELPER]})
        for minute in range(0, 4 * S + 1):
            self.text[HELPER] = f'revmux: {minute} reviewers done'
            self.tick(minute)
        self.assertEqual([], self.stall_mail())

    def test_the_humans_review_is_always_live_even_when_quiet(self):
        self.sessions.append({'id': 'h2', 'name': '#7 your review', 'paneIds': [HELPER]})
        self.text[HELPER] = 'revdiff'
        self.assert_quiet(6 * S)

    def test_a_pr_open_for_review(self):
        self.r.state['pr'] = {'number': 9, 'state': 'OPEN'}
        self.assert_quiet()

    def test_a_pr_open_under_auto_merge_is_still_watched(self):
        self.r.state['pr'] = {'number': 9, 'state': 'OPEN'}
        self.write('implementer.json', {'tool': 'codex', 'autoMerge': True})
        self.run_until(S)
        self.assertEqual(1, len(self.stall_mail()))

    def test_under_auto_merge_running_ci_exempts_and_finished_ci_does_not(self):
        # r1 F2: the planner waits on CI with a background wait-ci the relay cannot see.
        self.write('implementer.json', {'tool': 'codex', 'autoMerge': True})
        done = {'__typename': 'CheckRun', 'name': 'tests', 'status': 'COMPLETED', 'conclusion': 'SUCCESS'}
        for pending in ({'__typename': 'CheckRun', 'name': 'build', 'status': 'IN_PROGRESS'},
                        {'__typename': 'CheckRun', 'name': 'build', 'status': 'QUEUED'},
                        {'__typename': 'StatusContext', 'context': 'ci/x', 'state': 'PENDING'},
                        {'__typename': 'StatusContext', 'context': 'ci/x', 'state': 'EXPECTED'}):
            with self.subTest(pending=pending):
                self.r = self.make_relay()
                self.r.state['pr'] = {'number': 9, 'state': 'OPEN', 'statusCheckRollup': [done, pending]}
                self.assert_quiet()
                self.assertTrue(any('CI running' in line for line in self.logs), self.logs)
        self.r = self.make_relay()
        self.r.state['pr'] = {'number': 9, 'state': 'OPEN', 'statusCheckRollup': [
            done, {'__typename': 'StatusContext', 'context': 'ci/x', 'state': 'FAILURE'}]}
        self.run_until(S)
        self.assertEqual(1, len(self.stall_mail()))

    def test_the_rollup_adds_no_pr_events(self):
        self.assertIn('statusCheckRollup', relay.PR_FIELDS.split(','))
        base = {'number': 9, 'state': 'OPEN', 'url': 'u', 'reviews': [], 'comments': [], 'inline': [],
                'reviewDecision': '', 'statusCheckRollup': [{'__typename': 'CheckRun', 'name': 'b', 'status': 'QUEUED'}]}
        later = dict(base, statusCheckRollup=[{'__typename': 'CheckRun', 'name': 'b', 'status': 'COMPLETED'}])
        self.assertEqual([], relay.pr_events(base, later))

    def test_loop_reports_and_records(self):
        for name, data in (('loop.json', {'state': 'pr-open', 'rev': 1}), ('loop.json', {'state': 'blocked', 'rev': 2}),
                           ('loop-done.json', {'pr': 9}), ('waiting.json', {'by': 'planner'})):
            with self.subTest(name=name, data=data):
                self.r = self.make_relay()
                self.write(name, data)
                self.assert_quiet()
                (self.hub_dir / 'state' / name).unlink()

    def test_unread_mail_in_either_box(self):
        for box in ('claude', 'codex'):
            with self.subTest(box=box):
                self.r = self.make_relay()
                path = self.mail(box, sender='human' if box == 'claude' else 'claude')
                self.assert_quiet()
                path.unlink()

    def test_panes_not_provably_idle(self):
        for pane, frame in ((PLANNER, CLAUDE_RUNNING), (PLANNER, claude('a draft')), (IMPLEMENTER, codex('a draft')),
                            (IMPLEMENTER, agw.CtlError('no pipe')), (IMPLEMENTER, 'plain shell PS C:\\>')):
            with self.subTest(pane=pane, frame=str(frame)[:30]):
                self.r = self.make_relay()
                saved = self.text[pane]
                self.text[pane] = frame
                self.assert_quiet()
                self.text[pane] = saved

    def test_a_usage_limit_episode(self):
        self.r.state['limits'] = {'codex': {'kind': 'limited', 'tool': 'codex', 'announced': True}}
        self.assert_quiet()

    def test_a_usage_limit_waited_out(self):
        # #77: hours of waiting are not a stall.
        self.r.state['limits'] = {'codex': {'kind': 'limited', 'tool': 'kimi', 'announced': True, 'wait': True,
                                            'retryAt': 1_000_000 + 30 * MIN, 'probes': 3, 'probeAt': None}}
        self.assert_quiet(20 * S)

    def test_a_review_usage_limit_wait_until_its_retry_time(self):
        # #77: a reviewer's limit stopped a revmux round; the planner waits for the rerun with nothing live.
        self.write('review-limit.json', {'tool': 'kimi', 'since': 1_000_000, 'retryAt': 1_000_000 + 8 * S * MIN,
                                         'round': 2})
        self.assert_quiet(8 * S - 1)
        self.assertTrue(any('review usage-limit wait until' in line for line in self.logs), self.logs)
        self.run_until(9 * S, start=8 * S)                  # past retryAt with no rerun running: a stall
        self.assertEqual(1, len(self.stall_mail()))

    def test_an_unreadable_review_limit_retry_time_still_exempts(self):
        self.write('review-limit.json', {'tool': 'kimi', 'round': 2})
        self.assert_quiet()

    def test_off_when_stall_minutes_is_zero(self):
        self.r = self.make_relay(minutes=0)
        self.assert_quiet()

    def test_dry_run_only_logs(self):
        self.r = self.make_relay(dry_run=True)
        self.run_until(4 * S)
        self.assertEqual([], self.stall_mail())
        self.assertEqual([], self.escalations())
        self.assertTrue(any('[dry-run] would mail the planner: stall:' in line for line in self.logs), self.logs)
        self.assertTrue(any('[dry-run] would report the loop blocked: stalled:' in line for line in self.logs))


class KimiLimitFallback(StallFixture):
    """#88: a Kimi implementer stalled on a limit error the classifier could not place (docxy #775). In a
    -WaitOnLimit checkout the stall watch starts the wait the limit check missed; nobody is pointed at it."""

    def setUp(self):
        super().setUp()
        self.r.peers[1] = relay.Peer('codex', 'kimi', IMPLEMENTER)
        frame = Path(__file__).resolve().parent / 'fixtures' / 'limits' / 'kimi-tool-output-last.txt'
        self.text[IMPLEMENTER] = frame.read_text(encoding='utf-8')
        self.assertIsNone(relay.limits.classify(self.text[IMPLEMENTER], 'kimi'))

    def on_limit(self, value):
        self.write('implementer.json', {'tool': 'kimi', 'onLimit': value})

    def waiting_notes(self):
        box = self.hub_dir / 'inbox' / 'claude'
        return [m for m in (hub.parse_message(p) for p in sorted(box.glob('*.md')))
                if m.get('from') == 'relay' and m.get('subject') == 'usage limit: codex (kimi) waiting']

    def test_wait_mode_starts_a_forced_wait_instead_of_the_pointer(self):
        self.on_limit('wait')
        self.run_until(4 * S)
        self.assertEqual([], self.stall_mail())
        self.assertEqual([], self.escalations())
        episode = self.r.state['limits']['codex']
        self.assertEqual(('stall', True, True, 'kimi'), (episode['forced'], episode['wait'], episode['announced'],
                                                         episode['tool']))
        self.assertTrue(episode['line'].startswith('Error: [provider.api_error]'), episode['line'])
        self.assertEqual(1, len(self.waiting_notes()))
        saved = json.loads((self.hub_dir / 'state' / 'relay.json').read_text(encoding='utf-8'))
        self.assertEqual('stall', saved['limits']['codex']['forced'])

    def test_failover_mode_mails_the_pointer_with_the_limit_row(self):
        self.on_limit('failover')
        self.run_until(S)
        mails = self.stall_mail()
        self.assertEqual(1, len(mails))
        self.assertIn("The implementer's pane shows a usage-limit error: Error: [provider.api_error]", mails[0]['body'])
        self.assertNotIn('limits', self.r.state)

    def test_no_limit_on_screen_keeps_the_pointer(self):
        self.on_limit('wait')
        kimi = Path(__file__).resolve().parent / 'fixtures' / 'kimi' / 'idle-after-turn.txt'
        self.text[IMPLEMENTER] = kimi.read_text(encoding='utf-8')
        self.run_until(S)
        self.assertEqual(1, len(self.stall_mail()))
        self.assertNotIn('usage-limit error', self.stall_mail()[0]['body'])
        self.assertNotIn('limits', self.r.state)

    def test_a_codex_implementer_keeps_the_pointer(self):
        self.on_limit('wait')
        self.r.peers[1] = relay.Peer('codex', 'codex', IMPLEMENTER)
        self.text[IMPLEMENTER] = CODEX_IDLE
        self.run_until(S)
        self.assertEqual(1, len(self.stall_mail()))
        self.assertNotIn('limits', self.r.state)


class HeldMail(StallFixture):
    """#88 reopened (docxy #820): mail to a Kimi implementer at its usage limit wakes nothing - the relay
    rang it and its turn failed - so it is held, not an exemption; the stall period still applies."""

    def setUp(self):
        super().setUp()
        self.r.peers[1] = relay.Peer('codex', 'kimi', IMPLEMENTER)
        frame = Path(__file__).resolve().parent / 'fixtures' / 'limits' / 'kimi-limited-5hour-todo.txt'
        self.text[IMPLEMENTER] = frame.read_text(encoding='utf-8')
        self.held = Path(self.mail('codex')).stem

    def on_limit(self, value):
        self.write('implementer.json', {'tool': 'kimi', 'onLimit': value})

    def test_held_mail_is_not_an_exemption(self):
        texts = self.r.read_panes()
        self.assertEqual([], self.r.stall.exemptions([], texts))
        self.assertEqual([f'codex/{self.held}'], self.r.stall.held)
        self.assertEqual([f'unread mail codex/{self.held}'], self.r.stall.exemptions([]))   # no pane texts: as before
        self.assertTrue(any(f'unread mail codex/{self.held} is held for the limited implementer' in line
                            for line in self.logs), self.logs)

    def test_wait_mode_starts_a_forced_wait(self):
        self.on_limit('wait')
        self.run_until(4 * S)
        self.assertEqual([], self.stall_mail())
        self.assertEqual([], self.escalations())
        episode = self.r.state['limits']['codex']
        self.assertEqual(('stall', True), (episode['forced'], episode['wait']))
        self.assertTrue(episode['line'].startswith('Error: [provider.auth_error] 403'), episode['line'])

    def test_failover_mode_points_at_the_limit_and_the_held_mail(self):
        self.on_limit('failover')
        self.run_until(S)
        mails = self.stall_mail()
        self.assertEqual(1, len(mails))
        self.assertIn('mail held for the limited implementer', mails[0]['subject'])
        self.assertNotIn('nothing unread', mails[0]['subject'])
        self.assertNotIn('no unread mail in either box', mails[0]['body'])
        self.assertIn(f'unread mail codex/{self.held} is held for the implementer at its usage limit', mails[0]['body'])
        self.assertIn("The implementer's pane shows a usage-limit error: Error: [provider.auth_error] 403", mails[0]['body'])
        self.assertNotIn('Todo', mails[0]['body'])

    def test_a_relay_restarted_mid_limit_waits_it_out(self):
        # The limit row was on screen when this relay started: the limit check keeps it as history, so only
        # the stall watch's forced wait - which no baseline suppresses - can start the episode.
        self.on_limit('wait')
        for minute in range(0, 2 * S + 1):
            self.t = minute * MIN
            texts = self.r.read_panes()
            self.r.check_limits(texts)
            self.r.stall.tick(texts)
            if minute < S:
                self.assertNotIn('limits', self.r.state)
        self.assertEqual(1, len(self.r.limit_baseline['codex']))
        self.assertEqual('stall', self.r.state['limits']['codex']['forced'])
        self.assertEqual([], self.stall_mail())

    def test_unread_planner_mail_still_exempts(self):
        self.on_limit('wait')
        self.mail('claude', sender='human')
        self.run_until(4 * S)
        self.assertEqual([], self.stall_mail())
        self.assertNotIn('limits', self.r.state)

    def test_implementer_mail_without_a_limit_on_screen_still_exempts(self):
        self.on_limit('wait')
        kimi = Path(__file__).resolve().parent / 'fixtures' / 'kimi' / 'idle-after-turn.txt'
        self.text[IMPLEMENTER] = kimi.read_text(encoding='utf-8')
        self.run_until(4 * S)
        self.assertEqual([], self.stall_mail())
        self.assertEqual([], self.r.stall.held)
        self.assertTrue(any(f'not stalled: unread mail codex/{self.held}' in line for line in self.logs), self.logs)


class QuietHelper(StallFixture):
    def test_a_marker_less_helper_quiet_for_two_periods_stops_exempting_and_is_named(self):
        # G2: a helper killed under memory pressure leaves its pane on screen and never writes a marker.
        self.sessions.append({'id': 'h1', 'name': '#7 suite abc1234', 'paneIds': [HELPER]})
        self.text[HELPER] = 'Ran 12 tests ... (killed)'
        self.run_until(2 * S + S - 1)
        self.assertEqual([], self.stall_mail())
        self.run_until(3 * S + 1, start=3 * S)
        mails = self.stall_mail()
        self.assertEqual(1, len(mails))
        self.assertIn('helper #7 suite abc1234 has no completion marker and its pane has not changed for', mails[0]['body'])

    def test_a_finished_helper_does_not_exempt(self):
        self.sessions.append({'id': 'h1', 'name': '#7 suite abc1234', 'paneIds': [HELPER]})
        (self.hub_dir / 'state' / 'helpers').mkdir()
        (self.hub_dir / 'state' / 'helpers' / f'{HELPER}.done').write_text('{}', encoding='utf-8')
        self.run_until(S)
        self.assertEqual(1, len(self.stall_mail()))


class CiBackstop(StallFixture):
    """#94: finished CI on the open PR's head that no `ci` mail named is the stall; the watch queues that
    mail. Defensive: watch_pr queues it in the same save as the snapshot, so only state whose `ci_mailed`
    was lost while the snapshot survived (hand-edited) reaches this; these tests plant such state. It
    files only a finished CI result, never the time-based no-checks mail (#94 r3)."""

    FINISHED = {'number': 9, 'url': 'https://github.com/o/repo/pull/9', 'state': 'OPEN', 'headRefOid': 'c' * 40,
                'statusCheckRollup': [
                    {'__typename': 'CheckRun', 'name': 'tests', 'status': 'COMPLETED', 'conclusion': 'SUCCESS'},
                    {'__typename': 'StatusContext', 'context': 'ci/x', 'state': 'FAILURE'}]}

    def setUp(self):
        super().setUp()
        self.write('implementer.json', {'tool': 'codex', 'autoMerge': True})
        self.r.state['pr'] = dict(self.FINISHED)

    def ci_mail(self):
        box = self.hub_dir / 'inbox' / 'claude'
        return [message for message in map(hub.parse_message, sorted(box.glob('*.md')))
                if message.get('kind') == 'ci']

    def test_an_unmailed_finished_ci_files_the_ci_mail_not_the_pointer(self):
        self.run_until(S)
        mails = self.ci_mail()
        self.assertEqual(1, len(mails))
        self.assertEqual(('claude', 'relay'), (mails[0]['to'], mails[0]['from']))
        self.assertEqual('CI finished on ccccccc: 1 passed, 1 failed (ci/x)', mails[0]['subject'])
        self.assertEqual(relay.ci_result(self.FINISHED)['key'], self.r.state['ci_mailed'])
        self.assertEqual([], self.stall_mail())
        # Unread, it exempts the loop; the stall watch files it only once.
        self.run_until(3 * S, start=S + 1)
        self.assertEqual(1, len(self.ci_mail()))
        self.assertEqual([], self.stall_mail())

    def test_a_stale_snapshot_without_checks_gets_the_pointer_not_a_no_checks_mail(self):
        # #94 r3: gh failed for a stall period after a poll saved an empty rollup; checks may be running now.
        self.r.state['pr'] = dict(self.FINISHED, statusCheckRollup=[])
        self.r.no_checks = ((9, 'c' * 40), 0.0)      # watch_pr saw that head without checks at minute 0
        self.run_until(S)
        self.assertEqual([], self.ci_mail())
        self.assertNotIn('ci_mailed', self.r.state)
        self.assertEqual(1, len(self.stall_mail()))

    def test_a_ci_mail_that_was_not_filed_falls_through_to_the_pointer(self):
        with patch.object(self.r, 'ci_mail', return_value=False):
            self.run_until(S)
        self.assertEqual(1, len(self.stall_mail()))

    def test_ci_already_mailed_and_read_the_pointer_says_what_was_missed(self):
        self.r.state['ci_mailed'] = relay.ci_result(self.FINISHED)['key']
        self.run_until(S)
        self.assertEqual([], self.ci_mail())
        mails = self.stall_mail()
        self.assertEqual(1, len(mails))
        self.assertIn('CI on the PR head: CI finished on ccccccc: 1 passed, 1 failed (ci/x)', mails[0]['body'])

    def test_dry_run_files_nothing(self):
        self.r = self.make_relay(dry_run=True)
        self.r.state['pr'] = dict(self.FINISHED)
        self.run_until(S)
        self.assertEqual([], self.ci_mail())
        self.assertNotIn('ci_mailed', self.r.state)


class CiStuck(StallFixture):
    """#94 r1: with wait-ci optional, a check stuck pending (an offline runner, a status that never
    reports) must not exempt the loop forever: a pending set unchanged for CI_STUCK_MINUTES ends it."""

    BOUND = int(relay.CI_STUCK_MINUTES)

    def setUp(self):
        super().setUp()
        self.write('implementer.json', {'tool': 'codex', 'autoMerge': True})
        self.pending(('build',))

    def pending(self, names, head='c' * 40):
        self.r.state['pr'] = {'number': 9, 'state': 'OPEN', 'headRefOid': head, 'statusCheckRollup': [
            {'__typename': 'CheckRun', 'name': 'tests', 'status': 'COMPLETED', 'conclusion': 'SUCCESS'}] + [
            {'__typename': 'CheckRun', 'name': name, 'status': 'QUEUED'} for name in names]}

    def test_a_pending_set_unchanged_past_the_bound_is_pointed_at_then_escalated(self):
        self.assertEqual(90, self.BOUND)
        self.run_until(self.BOUND + S - 1, step=1)
        self.assertEqual([], self.stall_mail())
        self.tick(self.BOUND + S)
        mails = self.stall_mail()
        self.assertEqual(1, len(mails))
        self.assertIn('CI on PR #9 stuck: build pending, unchanged for 105 min', mails[0]['body'])
        self.run_until(self.BOUND + 4 * S, start=self.BOUND + S + 1)
        self.assertEqual(1, len(self.escalations()))
        # #94 r2: the human hears why - waiting.json, the notification.
        reason = json.loads((self.hub_dir / 'state' / 'waiting.json').read_text(encoding='utf-8'))['reason']
        self.assertIn('; CI on PR #9 stuck: build pending, unchanged for', reason)
        self.assertIn('CI on PR #9 stuck: build pending', self.notify.call_args.args[1])

    def test_the_stuck_note_is_logged_once(self):
        # #94 r2: not every tick, though another exemption (waiting.json) alternates with it.
        self.run_until(self.BOUND + 5)
        self.write('waiting.json', {'by': 'planner'})
        self.run_until(self.BOUND + 20, start=self.BOUND + 6)
        self.assertEqual(1, sum('CI on PR #9 stuck' in line for line in self.logs))

    def test_a_changing_pending_set_restarts_the_bound(self):
        self.run_until(60)
        self.pending(('build', 'lint'))
        self.run_until(60 + self.BOUND - 1, start=61)
        self.assertEqual([], self.stall_mail())
        # The clock restarts on the first tick that sees the new set (minute 61).
        self.run_until(61 + self.BOUND + S, start=60 + self.BOUND)
        self.assertEqual(1, len(self.stall_mail()))

    def test_a_new_head_restarts_the_bound(self):
        self.run_until(60)
        self.pending(('build',), head='d' * 40)
        self.run_until(60 + self.BOUND + S - 1, start=61)
        self.assertEqual([], self.stall_mail())

    def test_finished_ci_clears_the_stuck_note(self):
        self.run_until(self.BOUND)
        self.assertIsNotNone(self.r.stall.ci_stuck)
        self.r.state['pr']['statusCheckRollup'] = self.r.state['pr']['statusCheckRollup'][:1]
        self.r.state['ci_mailed'] = relay.ci_result(self.r.state['pr'])['key']
        self.tick(self.BOUND + 1)
        self.assertIsNone(self.r.stall.ci_stuck)
        self.assertIsNone(self.r.stall.ci_wait)


class LastWords(unittest.TestCase):
    def test_codex_last_paragraph_above_the_composer_not_the_footer(self):
        self.assertEqual('• No unread mail. Waiting for the next “Chat from Workbench:” notification; no files edited.',
                         relay.last_words(CODEX_IDLE, 'codex'))

    def test_claude_answer_above_the_turn_timer(self):
        frame = CLAUDE_IDLE.replace('✻ Brewed', '● Should the relay also watch builds? I need the human to\n'
                                               '  decide before I go on.\n\n✻ Brewed')
        self.assertEqual('● Should the relay also watch builds? I need the human to decide before I go on.',
                         relay.last_words(frame, 'claude'))

    def test_nothing_recognisable(self):
        self.assertIsNone(relay.last_words('PS C:\\> ', 'codex'))
        self.assertIsNone(relay.last_words(CLAUDE_IDLE, 'claude'))      # only the turn timer above the box
        self.assertIsNone(relay.last_words(None, 'claude'))

    def test_kimi_last_paragraph_above_its_box_past_the_spinner(self):
        # #65: captured Kimi Code frames (tests/fixtures/kimi)
        kimi = lambda name: (Path(__file__).resolve().parent / 'fixtures' / 'kimi' / f'{name}.txt').read_text(encoding='utf-8')
        self.assertEqual('● done', relay.last_words(kimi('idle-after-turn'), 'kimi'))
        self.assertEqual('● Running a command · $ sleep 25 && echo slept', relay.last_words(kimi('running-tool'), 'kimi'))
        self.assertIsNone(relay.last_words(kimi('approval'), 'kimi'))       # no composer box: a dialog
        self.assertIsNone(relay.last_words(CLAUDE_IDLE, 'kimi'))

    def test_kimi_last_paragraph_above_its_todo_panel(self):
        # #88 reopened: docxy #820's stall pointer quoted the todo panel instead of the limit error.
        frame = (Path(__file__).resolve().parent / 'fixtures' / 'limits' / 'kimi-limited-5hour-todo.txt').read_text(encoding='utf-8')
        words = relay.last_words(frame, 'kimi')
        self.assertTrue(words.startswith('…') and words.endswith("Please don't share it publicly."), words)
        self.assertIn('the current 5-hour window ends', words)      # clipped from the front to LAST_WORDS_MAX
        self.assertNotIn('Todo', words)

    def test_kimi_idle_blockers(self):
        kimi = lambda name: (Path(__file__).resolve().parent / 'fixtures' / 'kimi' / f'{name}.txt').read_text(encoding='utf-8')
        peer = relay.Peer('codex', 'kimi', 'pane')
        self.assertEqual([], closer.idle_blockers(peer, kimi('idle-after-turn')))
        self.assertEqual(['codex is running a turn'], closer.idle_blockers(peer, kimi('running-thinking')))
        self.assertEqual(['codex composer is not provably empty'], closer.idle_blockers(peer, kimi('draft-wrapped')))
        self.assertEqual(['codex composer is not provably empty'], closer.idle_blockers(peer, kimi('approval')))

    def test_a_long_line_is_clipped_from_the_front(self):
        words = relay.last_words(codex('', prefix='• ' + 'x' * 500), 'codex')
        self.assertEqual(relay.LAST_WORDS_MAX + 1, len(words))
        self.assertTrue(words.startswith('…'))


class SuiteHelperSessions(unittest.TestCase):
    def test_suite_sessions_are_helpers_the_close_knows(self):
        tree = {'workspaces': [{'name': 'repo', 'sessions': [
            {'id': 'a', 'name': '#7 suite 1f04542'}, {'id': 'b', 'name': '#7 suite bad label!'},
            {'id': 'c', 'name': '#7 revmux r2'}, {'id': 'd', 'name': '#7 your review'}, {'id': 'e', 'name': '#8 suite x'}]}]}
        close = closer.Closer(Path('hub'), 'o/repo', '7', [], log=lambda text: None)
        self.assertEqual(['a', 'c', 'd'], [session['id'] for session in close.helper_sessions(tree)])


class StallProse(unittest.TestCase):
    ROOT = Path(__file__).resolve().parent.parent

    def text(self, path):
        return ' '.join((self.ROOT / path).read_text(encoding='utf-8').split())

    def test_the_planner_knows_the_pointer_the_latch_and_the_suite_helper(self):
        text = self.text('claude/commands/start-github-issue.md')
        section = text.split('## Stall pointers')[1].split('## Adopted session')[0]
        for needle in ('kind `stall`', 'Check your background waiter', 'mail from `helper`',
                       '`.workbench/state/waiting.json`', 'That record is a latch',
                       'run `wb.py status active`', '`loop-state resumed`', 'reason starting `stalled:`',
                       'no CI still running on an auto-merge PR', 'no usage-limit episode'):
            self.assertIn(needle, section)
        rules = text.split('## Rules')[1]
        self.assertIn('wb.py" suite --label <sha7> -- <command and its arguments>', rules)
        self.assertIn('never with a hand-rolled watcher', rules)
        self.assertIn('run `wb.py status active` when you resume', rules)
        self.assertIn('`wb.py suite --label <sha7> -- <command>`', text.split('## Phase 6')[1].split('## Phase 7')[0])

    def test_the_planner_knows_the_relays_ci_mail(self):
        # #94: the relay's `ci` mail stands in for a killed wait-ci.
        text = self.text('claude/commands/start-github-issue.md')
        section = text.split('## CI results')[1].split('## Stall pointers')[0]
        for needle in ('from `relay`, kind `ci`', 'same signal as wait-ci\'s exit 0', '`wait-ci` is optional',
                       'Read every `ci` mail', 'merge-check decides which checks are required',
                       'stop any wait-ci still running for that head', 'mail for another head is ignored',
                       'Without auto-merge: mention a red result in chat'):
            self.assertIn(needle, section)
        self.assertIn("the relay's `ci` mail for this head", text.split('| `ci-pending:` |')[1].split('|')[0])
        self.assertIn('`CI on the PR head: <summary>`', text.split('## Stall pointers')[1])
        self.assertIn('unchanged for 90 minutes (wait-ci\'s timeout)', section)
        self.assertNotIn('push, wait-ci, merge-check', text)
        self.assertNotIn('then wait-ci and merge-check', text)
        readme = self.text('README.md')
        self.assertIn('no checks reported in 5 min', section)
        for needle in ('no checks reported in 5 min', 'unchanged for 90 minutes, wait-ci\'s timeout', "**The relay's CI mail (#94).**", 'once per finished run, not once per head',
                       'A check that registers late', '`CI on the PR head: ...`'):
            self.assertIn(needle, readme)

    def test_implementers_never_hide_a_suite_in_a_background_watcher(self):
        self.assertIn('wb.py" suite --label <sha7> -- <command>`', self.text('claude/commands/workbench-implementer.md'))
        for path in ('claude/commands/workbench-implementer.md', 'codex/skills/workbench-implementer/SKILL.md',
                     'kimi/AGENTS.md'):
            with self.subTest(path=path):
                self.assertIn('looks like a stalled loop to the relay', self.text(path))

    def test_every_implementer_role_knows_a_final_round(self):
        # #64 / #65 FIX r3: the Kimi role (kimi/AGENTS.md) keeps up with the Codex and Claude ones.
        for path in ('claude/commands/workbench-implementer.md', 'codex/skills/workbench-implementer/SKILL.md',
                     'kimi/AGENTS.md'):
            with self.subTest(path=path):
                text = ' '.join(self.text(path).split())
                self.assertIn('A `FIX r<K> (final)` round has no revmux round after it', text)
                self.assertIn('A Major never arrives in a final round.', text)


if __name__ == "__main__":
    unittest.main()
