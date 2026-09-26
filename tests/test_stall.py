"""#45: the relay's stall watch, with a fake clock, a stub terminal and a temp mailbox.

A loop that sits idle with nothing to wake it gets one `stall` pointer after stallMinutes, and is
reported blocked after two more periods with no progress. Nothing that means "the loop is waiting
correctly" may raise one.
"""

from __future__ import annotations

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


class Escalation(StallFixture):
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


class NoFalseStalls(StallFixture):
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
                       'run `wb.py status active`', '`loop-state resumed`', 'reason starting `stalled:`'):
            self.assertIn(needle, section)
        rules = text.split('## Rules')[1]
        self.assertIn('wb.py" suite --label <sha7> -- <command and its arguments>', rules)
        self.assertIn('never with a hand-rolled watcher', rules)
        self.assertIn('run `wb.py status active` when you resume', rules)
        self.assertIn('`wb.py suite --label <sha7> -- <command>`', text.split('## Phase 6')[1].split('## Phase 7')[0])

    def test_implementers_never_hide_a_suite_in_a_background_watcher(self):
        self.assertIn('wb.py" suite --label <sha7> -- <command>`', self.text('claude/commands/workbench-implementer.md'))
        for path in ('claude/commands/workbench-implementer.md', 'codex/skills/workbench-implementer/SKILL.md'):
            with self.subTest(path=path):
                self.assertIn('looks like a stalled loop to the relay', self.text(path))


if __name__ == "__main__":
    unittest.main()
