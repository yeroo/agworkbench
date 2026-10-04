"""#98: the relay's exit watch, with a fake clock, a stub terminal and a temp mailbox.

An agent that exits or crashes leaves its pane at the root shell's prompt. The relay restarts it with
the pane's pinned command after a grace period, types one resume pointer once the agent's composer is
up, and gives up (telling the human) after three restarts in an hour. Anything that is not provably a
dead agent at a bare prompt, or that someone else owns, is never typed into.
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
from frames import CLAUDE_IDLE, CLAUDE_RUNNING, CODEX_IDLE, codex  # noqa: E402

PLANNER, IMPLEMENTER = 'planner-pane', 'implementer-pane'
MIN = 60.0
FIXTURES = Path(__file__).resolve().parent / 'fixtures'
PROMPT = 'PS C:\\Users\\boris\\source\\workbench\\repo-issue-7> '
PINS = {PLANNER: 'pwsh -NoLogo -File C:\\agworkbench\\lib\\pane-claude.ps1 -Checkout C:\\repo-issue-7',
        IMPLEMENTER: 'pwsh -NoLogo -File C:\\agworkbench\\lib\\pane-implementer-kimi.ps1 -Checkout C:\\repo-issue-7 -Resume'}


def fixture(name):
    return (FIXTURES / name).read_text(encoding='utf-8')


def at_prompt(above='To resume this session: kimi -r session_0f0e2a55'):
    return above + '\n' + PROMPT


KIMI_IDLE = fixture('kimi/idle-fresh.txt')
# A command the human runs in the shell below a crashed agent's frame (FIX r3 M2).
PS_COMMAND = '\nPS C:\\repo> npm test\n\n> repo@1.0.0 test\nrunning 120 tests...\n'
BASH_COMMAND = '\nboris@host:~/repo$ npm test\n' + '\n'.join(f'  ok {i} - test' for i in range(20)) + '\n'
BASH_PROMPT = '\nboris@host:~/repo$ \n'
# Kimi's box with its one footer row, then a pwsh command waiting for input: three rows below the box.
_kimi_rows = KIMI_IDLE.splitlines()
_kimi_bottom = max(i for i, row in enumerate(_kimi_rows) if peerchat.KIMI_BOTTOM_RE.match(row))
KIMI_PS_FOOTER = '\n'.join(_kimi_rows[:_kimi_bottom + 2]) + '\nPS C:\\repo> Read-Host name\nname: \n'


class ExitFixture(unittest.TestCase):
    implementer_tool = 'kimi'

    def setUp(self):
        self.folder = Path(__file__).resolve().parent.parent / ('test exit ' + uuid.uuid4().hex)
        self.hub_dir = self.folder / '.workbench'
        (self.hub_dir / 'state').mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.addCleanup(hub.reload_paths)
        self.config = self.folder / 'config.json'
        self.config.write_text('{}', encoding='utf-8')
        # The human's real ~/.agworkbench.json never reaches a result here.
        self.enterContext(patch.dict(os.environ, {'AGWORKBENCH_CONFIG': str(self.config)}))
        self.t = 0.0
        self.enterContext(patch.object(relay, 'now', lambda: self.t))
        self.enterContext(patch.object(relay, 'wall', lambda: 1_000_000 + self.t))
        agent = KIMI_IDLE if self.implementer_tool == 'kimi' else CODEX_IDLE
        self.text = {PLANNER: CLAUDE_IDLE, IMPLEMENTER: agent}
        self.enterContext(patch.object(agw, 'pane_text', side_effect=self.pane_text))
        self.session = {'id': 'issue', 'name': '#7 fix', 'paneIds': [PLANNER, IMPLEMENTER],
                        'foregroundShells': [None, None], 'restoreCommands': dict(PINS)}
        self.tree_error = None
        self.enterContext(patch.object(agw, 'tree', side_effect=self.tree))
        self.typed = self.enterContext(patch.object(agw, 'type_into'))
        self.send = self.enterContext(patch.object(peerchat, 'send', return_value='submitted'))
        self.status = self.enterContext(patch.object(agw, 'set_status'))
        self.notify = self.enterContext(patch.object(agw, 'notify'))
        self.enterContext(patch.object(agw, 'request', side_effect=AssertionError('real terminal request')))
        self.r = self.make_relay()

    def make_relay(self, dry_run=False):
        peers = [relay.Peer('claude', 'claude', PLANNER), relay.Peer('codex', self.implementer_tool, IMPLEMENTER)]
        made = relay.Relay(self.hub_dir, peers, 'o/repo', 'issue-7-fix', 5, 60, dry_run=dry_run)
        self.logs = []
        made.log = self.logs.append
        return made

    def tree(self):
        if self.tree_error:
            raise self.tree_error
        return {'workspaces': [{'name': 'repo', 'sessions': [self.session]}]}

    def pane_text(self, pane):
        value = self.text[pane]
        if isinstance(value, Exception):
            raise value
        return value

    def exit(self, pane=IMPLEMENTER, text=None, shell='pwsh'):
        self.text[pane] = text if text is not None else at_prompt()
        self.session['foregroundShells'][self.session['paneIds'].index(pane)] = shell

    def agent(self, pane, text):
        self.text[pane] = text
        self.session['foregroundShells'][self.session['paneIds'].index(pane)] = None

    def tick(self, minute):
        self.t = minute * MIN
        self.r.exits.tick(self.r.read_panes())

    def run_until(self, last, start=0.0, step=0.5):
        minute = start
        while minute <= last:
            self.tick(minute)
            minute += step

    def restarts(self, pane=IMPLEMENTER):
        return [c for c in self.typed.call_args_list if c.args[0] == pane]

    def write(self, name, data):
        (self.hub_dir / 'state' / name).write_text(json.dumps(data), encoding='utf-8')

    def exit_mail(self):
        box = self.hub_dir / 'inbox' / 'claude'
        return [hub.parse_message(path) for path in sorted(box.glob('*.md'))
                if hub.parse_message(path).get('kind') == 'exit']

    def logged(self, part):
        return [line for line in self.logs if part in line]


class Restart(ExitFixture):
    def test_pwsh_prompt_restarted_after_grace_and_gets_pointer(self):
        self.exit()
        self.run_until(1.5)
        self.assertEqual([], self.restarts())                       # within the grace period
        self.tick(2)
        self.assertEqual(1, len(self.restarts()))
        self.assertEqual((IMPLEMENTER, PINS[IMPLEMENTER] + '\n'), self.restarts()[0].args)
        self.assertEqual(1, len(self.logged('agent exited; restarted with resume: codex (kimi) attempt 1/3')))
        self.assertEqual([1_000_000 + 2 * MIN], self.r.state['restarts']['codex'])
        self.send.assert_not_called()
        self.tick(2.5)                                               # still at the prompt: no pointer
        self.send.assert_not_called()
        self.agent(IMPLEMENTER, KIMI_IDLE)
        self.tick(3)
        self.send.assert_called_once()
        self.assertEqual(IMPLEMENTER, self.send.call_args.args[0])
        self.assertIn(relay.RESTART_TEXT, self.send.call_args.args[2])
        self.assertTrue(self.send.call_args.args[2].startswith('Chat from Workbench: '))
        self.run_until(30, start=3.5)
        self.assertEqual(1, len(self.restarts()))
        self.send.assert_called_once()
        self.assertEqual([], self.restarts(PLANNER))

    def test_planner_pane_restarted(self):
        self.exit(PLANNER)
        self.run_until(2)
        self.assertEqual([(PLANNER, PINS[PLANNER] + '\n')], [c.args for c in self.typed.call_args_list])
        self.assertEqual(1, len(self.logged('restarted with resume: claude (claude) attempt 1/3')))
        self.agent(PLANNER, CLAUDE_RUNNING)                          # the pin's own resume turn
        self.tick(2.5)
        self.send.assert_not_called()
        self.text[PLANNER] = CLAUDE_IDLE
        self.tick(3)
        self.assertEqual(PLANNER, self.send.call_args.args[0])

    def test_crashed_kimi_frame_restarted(self):
        # A crash runs no cleanup: Kimi's composer box and footer stay above the prompt.
        self.exit(text=at_prompt(fixture('kimi/idle-after-turn.txt')))
        self.run_until(2)
        self.assertEqual(1, len(self.restarts()))

    def test_crashed_claude_frame_restarted(self):
        self.exit(PLANNER, text=at_prompt(CLAUDE_IDLE))
        self.run_until(2)
        self.assertEqual(1, len(self.restarts(PLANNER)))

    def test_pointer_row_above_prompt_restarted(self):
        self.exit(text=at_prompt('Chat from Workbench: workbench mail from claude: plan v1 [id 1-claude-1]\n'
                                 'Chat from Workbench: : The term is not recognized'))
        self.run_until(2)
        self.assertEqual(1, len(self.restarts()))

    def test_kimi_exited_at_its_quota_is_restarted(self):
        # classify() leaves an exited Kimi to the planner (no episode), so the relay restarts it: the quota
        # then shows in Kimi's live composer, where the limit check sees it.
        self.exit(text=fixture('limits/kimi-exited-quoted.txt'))
        self.run_until(2)
        self.assertIsNone(relay.limits.classify(self.text[IMPLEMENTER], 'kimi'))
        self.assertEqual(1, len(self.restarts()))

    def test_tail_change_resets_grace(self):
        self.exit()
        self.run_until(1.5)
        self.text[IMPLEMENTER] = at_prompt('PS C:\\x> git status\nnothing to commit')   # a human at the shell
        self.run_until(3.5, start=2)                                 # the clock restarted at minute 2
        self.assertEqual([], self.restarts())
        self.tick(4)
        self.assertEqual(1, len(self.restarts()))

    def test_unreadable_reads_neither_advance_nor_reset_the_grace(self):
        self.exit()
        self.tick(0)
        self.text[IMPLEMENTER] = agw.CtlError('pipe busy')
        self.run_until(5, start=0.5)
        self.assertEqual([], self.restarts())
        self.exit()
        self.tick(5.5)                                               # same tail since minute 0
        self.assertEqual(1, len(self.restarts()))
        self.tree_error = OSError('no pipe')
        self.run_until(9, start=6)
        self.assertEqual(1, len(self.logged('terminal tree unreadable')))

    def test_a_restart_that_fails_waits_a_fresh_grace_and_counts(self):
        self.exit()
        self.run_until(2)
        self.exit(text=at_prompt('pane-implementer-kimi.ps1: kimi not found'))
        self.run_until(4, start=2.5)
        self.assertEqual(1, len(self.restarts()))
        self.tick(4.5)
        self.assertEqual(2, len(self.restarts()))
        self.assertEqual(1, len(self.logged('attempt 2/3')))

    def test_a_pane_that_changes_before_the_restart_is_not_typed_into(self):
        # FIX r2 m1: the tick's read is seconds old by the time the restart types; the pane is re-read.
        self.exit()
        self.run_until(1.5)
        texts = self.r.read_panes()
        self.t = 2 * MIN
        self.text[IMPLEMENTER] = at_prompt() + 'git st'              # the human starts typing meanwhile
        self.r.exits.tick(texts)
        self.assertEqual([], self.restarts())
        self.assertEqual(1, len(self.logged('changed just before its restart')))
        self.exit()                                                  # a fresh grace, then the restart
        self.run_until(4, start=2.5)
        self.assertEqual([], self.restarts())
        self.run_until(5, start=4.5)
        self.assertEqual(1, len(self.restarts()))

    def test_the_watch_is_wired_into_the_relay_loop(self):
        self.exit()
        calls = []
        self.enterContext(patch.object(self.r, 'check_limits', side_effect=lambda texts: calls.append('limits')))
        self.enterContext(patch.object(self.r.exits, 'tick', side_effect=lambda texts: calls.append('exits')))
        self.enterContext(patch.object(self.r.stall, 'tick', side_effect=lambda texts: calls.append('stall')))
        self.enterContext(patch.object(self.r, 'deliver_mail', side_effect=lambda: self.r.stop_file.touch()))
        self.enterContext(patch.object(self.r, 'sweep_helpers'))
        self.enterContext(patch.object(self.r, 'rescue_pointers'))
        self.r.run()
        self.assertEqual(['limits', 'exits', 'stall'], calls)

    def test_a_watch_bug_never_stops_the_relay(self):
        self.enterContext(patch.object(self.r, 'check_limits'))
        self.enterContext(patch.object(self.r.exits, 'tick', side_effect=KeyError('x')))
        stall = self.enterContext(patch.object(self.r.stall, 'tick'))
        self.enterContext(patch.object(self.r, 'deliver_mail', side_effect=lambda: self.r.stop_file.touch()))
        self.enterContext(patch.object(self.r, 'sweep_helpers'))
        self.enterContext(patch.object(self.r, 'rescue_pointers'))
        self.assertEqual(0, self.r.run())
        stall.assert_called_once()
        self.assertEqual(1, len(self.logged('exit watch failed: KeyError')))


class CodexRestart(ExitFixture):
    implementer_tool = 'codex'

    def test_codex_restarted(self):
        self.session['restoreCommands'][IMPLEMENTER] = 'pwsh -NoLogo -File pane-codex.ps1 -Resume'
        self.exit(text=at_prompt('To continue this session, run codex resume 0199aa11'))
        self.run_until(2)
        self.assertEqual([(IMPLEMENTER, 'pwsh -NoLogo -File pane-codex.ps1 -Resume\n')],
                         [c.args for c in self.typed.call_args_list])
        self.agent(IMPLEMENTER, CODEX_IDLE)
        self.tick(2.5)
        self.send.assert_called_once()

    def test_codex_exited_at_its_limit_is_the_limit_paths(self):
        self.r.check_limits(self.r.read_panes())                    # the relay saw Codex alive first
        self.exit(text=fixture('limits/codex-limited-exited.txt'))
        for minute in range(0, 6):
            self.t = minute * MIN
            texts = self.r.read_panes()
            self.r.check_limits(texts)
            self.r.exits.tick(texts)
        self.assertEqual([], self.typed.call_args_list)
        self.assertEqual(1, len(self.logged('not restarting codex (codex): a usage-limit episode owns it')))


class NotTouched(ExitFixture):
    def assert_untouched(self):
        self.assertEqual([], self.typed.call_args_list)
        self.send.assert_not_called()
        self.assertNotIn('restarts', self.r.state)

    def test_foreground_null_not_restarted(self):
        self.exit(shell=None)
        self.run_until(10)
        self.assert_untouched()

    def test_agent_frames_not_touched(self):
        frames = [fixture(f'kimi/{name}.txt') for name in ('approval', 'trust-dialog', 'draft-wrapped', 'shell-mode',
                                                            'running-tool', 'idle-after-turn')]
        frames += [CLAUDE_RUNNING, codex('fix the bug'), 'Overwrite? [y/N]', at_prompt() + 'git status',
                   at_prompt().replace('> ', '>  pwsh -File x.ps1')]
        for shell in (None, 'pwsh'):
            for frame in frames:
                with self.subTest(shell=shell, frame=frame[-60:]):
                    self.exit(text=frame, shell=shell)
                    self.run_until(self.t / MIN + 5, start=self.t / MIN + 0.5)
                    self.assert_untouched()

    def test_a_pane_missing_from_the_tree_is_unknown(self):
        self.exit()
        self.session['paneIds'] = [PLANNER, 'other-pane']
        self.run_until(10)
        self.assert_untouched()

    def test_no_foreground_shells_logged_once(self):
        self.exit()
        del self.session['foregroundShells']
        self.run_until(10)
        self.assert_untouched()
        self.assertEqual(1, len(self.logged('the terminal reports no foregroundShells; exited agents are not detected')))


class Guards(ExitFixture):
    def guarded(self, reason, minutes=10):
        restarts = self.r.state.get('restarts')
        self.exit()
        self.run_until(minutes)
        self.assertEqual([], self.typed.call_args_list)
        self.assertEqual(1, len(self.logged(reason)), self.logs)
        self.assertEqual(restarts, self.r.state.get('restarts'))

    def test_restart_exited_off(self):
        for text in ('{"restartExited": false}', '{"RestartExited": 0}', '{"restartExited": true, "RESTARTEXITED": false}'):
            with self.subTest(text=text):
                self.config.write_text(text, encoding='utf-8')
                self.r = self.make_relay()
                self.guarded('not restarting codex (kimi): restartExited is off')

    def test_unreadable_config_is_off(self):
        self.config.write_text('{"restartExited": tru', encoding='utf-8')
        self.guarded('restartExited is off (config')

    def test_restart_exited_on_or_null(self):
        for text in ('{"restartExited": true}', '{"restartExited": null}'):
            self.config.write_text(text, encoding='utf-8')
            self.assertEqual((True, ''), relay.restart_exited_setting())
        os.environ['AGWORKBENCH_CONFIG'] = str(self.folder / 'missing.json')
        self.assertEqual((True, ''), relay.restart_exited_setting())

    def test_launch_lock_held(self):
        lock = self.hub_dir / 'state' / 'launch.lock'
        lock.write_text('', encoding='utf-8')
        with patch('conductor.file_locked', side_effect=lambda path: path == lock):
            self.guarded('a launcher holds launch.lock')

    def test_loop_done(self):
        self.write('loop-done.json', {'pr': 7})
        self.guarded('the loop is done')

    def test_close_pending(self):
        self.r.state['close_pending'] = 7
        self.guarded('the close owns the panes')

    def test_draining(self):
        self.r.draining = True
        self.guarded('the close owns the panes')

    def test_limit_episode(self):
        self.r.state['limits'] = {'codex': {'kind': 'limited', 'tool': 'kimi', 'announced': True}}
        self.guarded('a usage-limit episode owns it')

    def test_implementer_json_records_the_tool_limited(self):
        # An unfinished failover: Clear-Host ended the relay's episode, the old pin is still there.
        self.write('implementer.json', {'tool': 'kimi', 'limits': {'kimi': {'line': 'quota', 'kind': 'limited'}}})
        self.guarded('state/implementer.json records kimi as limited')

    def test_implementer_json_names_another_tool(self):
        self.write('implementer.json', {'tool': 'claude'})
        self.guarded('state/implementer.json names claude but this relay rings kimi')

    def test_a_limit_on_another_tool_does_not_guard(self):
        self.write('implementer.json', {'tool': 'kimi', 'limits': {'codex': {'kind': 'limited'}}})
        self.exit()
        self.run_until(2)
        self.assertEqual(1, len(self.restarts()))

    def test_dry_run(self):
        self.r = self.make_relay(dry_run=True)
        self.guarded('[dry-run] would restart codex (kimi)')

    def assert_no_alert(self):
        self.notify.assert_not_called()
        self.status.assert_not_called()
        self.assertEqual([], self.exit_mail())
        self.assertFalse((self.hub_dir / 'state' / 'waiting.json').exists())
        self.assertFalse((self.hub_dir / 'state' / 'loop.json').exists())

    def test_dry_run_with_no_pin_alerts_nobody(self):
        # FIX r1 M1
        self.r = self.make_relay(dry_run=True)
        del self.session['restoreCommands'][IMPLEMENTER]
        self.guarded('[dry-run] would alert (no pinned restore command for codex (kimi))')
        self.assert_no_alert()

    def test_dry_run_with_the_budget_spent_gives_up_on_nothing(self):
        # FIX r1 M1: a dry run reading a live relay.json must not block the real loop.
        self.write('queue-member.json', {'queue': str(self.folder / 'queue.json'), 'repo': 'o/repo', 'number': 7})
        self.write('claude.json', {'sessionId': str(uuid.uuid4())})
        (self.hub_dir / 'state' / 'relay.json').write_text(json.dumps(
            {'restarts': {'codex': [1_000_000 + 0.0, 1_000_000 + 60.0, 1_000_000 + 120.0]}}), encoding='utf-8')
        self.r = self.make_relay(dry_run=True)
        self.guarded('[dry-run] would give up on codex (kimi) (3 restarts in the last hour)')
        self.assert_no_alert()

    def test_guard_lifting_lets_the_restart_happen(self):
        self.write('loop-done.json', {'pr': 7})
        self.exit()
        self.run_until(5)
        (self.hub_dir / 'state' / 'loop-done.json').unlink()
        self.tick(5.5)
        self.assertEqual(1, len(self.restarts()))


class Alerts(ExitFixture):
    def test_no_pin_alerts_once(self):
        del self.session['restoreCommands'][IMPLEMENTER]
        self.exit()
        self.run_until(20)
        self.assertEqual([], self.typed.call_args_list)
        self.assertEqual(1, len(self.logged('ALERT agent exited and its pane has no pinned restore command')))
        self.notify.assert_called_once()
        self.assertEqual(PLANNER, self.notify.call_args.args[0])
        self.assertEqual(1, len(self.exit_mail()))

    def test_no_pin_on_the_planner_notifies_without_mail(self):
        del self.session['restoreCommands'][PLANNER]
        self.exit(PLANNER)
        self.run_until(5)
        self.notify.assert_called_once()
        self.assertEqual([], self.exit_mail())

    def cycle(self, start):
        """One exit restarted at start + 2, the agent back at start + 3."""
        self.exit()
        self.run_until(start + 2, start=start)
        self.agent(IMPLEMENTER, KIMI_IDLE)
        self.run_until(start + 4, start=start + 2.5)

    def test_budget_three_per_hour(self):
        for start in (0, 10, 20):
            self.cycle(start)
        self.assertEqual(3, len(self.restarts()))
        self.exit()
        self.run_until(40, start=30)
        self.assertEqual(3, len(self.restarts()))
        alert = 'ALERT agent exited 4 times in an hour: codex (kimi); the relay does not restart it again'
        self.assertEqual(1, len(self.logged(alert)))
        self.status.assert_called_once_with('blocked', sound=True, blink=True, pane_id=PLANNER)
        self.notify.assert_called_once()
        waiting = json.loads((self.hub_dir / 'state' / 'waiting.json').read_text(encoding='utf-8'))
        self.assertEqual('relay', waiting['by'])
        self.assertIn('exited 4 times', waiting['reason'])
        mails = self.exit_mail()
        self.assertEqual(1, len(mails))
        self.assertEqual(('relay', 'exit'), (mails[0]['from'], mails[0]['kind']))
        self.assertFalse((self.hub_dir / 'state' / 'loop.json').exists())
        # FIX r1 M2: giving up is final for the episode, also once the oldest restart leaves the hour.
        self.run_until(90, start=40.5)
        self.assertEqual(3, len(self.restarts()))
        self.assertEqual(1, len(self.logged('exited 4 times')))

    def test_a_new_episode_after_the_hour_restarts_again(self):
        for start in (0, 10, 20):
            self.cycle(start)
        self.exit()
        self.run_until(40, start=30)
        self.assertEqual(3, len(self.restarts()))
        self.agent(IMPLEMENTER, KIMI_IDLE)                           # the human restarted it
        self.run_until(41, start=40.5)
        self.exit()
        self.run_until(70, start=60)                                 # the restart at minute 2 left the hour at 62
        self.assertEqual(4, len(self.restarts()))
        self.assertEqual(2, len(self.logged('attempt 3/3')))

    def test_budget_survives_a_relay_restart(self):
        for start in (0, 10, 20):
            self.cycle(start)
        self.r = self.make_relay()
        self.exit()
        self.run_until(40, start=30)
        self.assertEqual(3, len(self.restarts()))
        self.assertEqual(1, len(self.logged('exited 4 times in an hour')))

    def test_queue_mode_environmental_block(self):
        loop_id = str(uuid.uuid4())
        self.write('queue-member.json', {'queue': str(self.folder / 'queue.json'), 'repo': 'o/repo', 'number': 7})
        self.write('claude.json', {'sessionId': loop_id})
        self.r.state['restarts'] = {'codex': [1_000_000 + 0.0, 1_000_000 + 60.0, 1_000_000 + 120.0]}
        self.exit()
        self.run_until(5)
        loop = json.loads((self.hub_dir / 'state' / 'loop.json').read_text(encoding='utf-8'))
        self.assertEqual(('blocked', 'environment', loop_id), (loop['state'], loop['cause'], loop['loopId']))
        self.assertIn('exited 4 times', loop['reason'])

    def test_episode_ends_after_two_reads(self):
        self.r.state['restarts'] = {'codex': [1_000_000 + 0.0, 1_000_000 + 60.0, 1_000_000 + 120.0]}
        self.exit()
        self.run_until(5)
        self.assertEqual(1, len(self.logged('exited 4 times')))
        self.agent(IMPLEMENTER, KIMI_IDLE)                           # one read back: the episode stays
        self.tick(5.5)
        self.exit()
        self.run_until(10, start=6)
        self.assertEqual(1, len(self.logged('exited 4 times')))
        self.agent(IMPLEMENTER, KIMI_IDLE)                           # two reads back: it ends
        self.run_until(11, start=10.5)
        self.assertEqual(1, len(self.logged('codex is no longer at a shell prompt')))
        self.exit()
        self.run_until(15, start=11.5)
        self.assertEqual(2, len(self.logged('exited 4 times')))


class Latches(ExitFixture):
    """FIX r2 M1: giving up and the no-pin alert hold until an agent is seen in the pane again, whatever
    the human runs in its shell meanwhile, and across a relay restart."""
    SPENT = [1_000_000 + 0.0, 1_000_000 + 60.0, 1_000_000 + 120.0]    # the hour frees up at minute 62

    def give_up(self):
        self.r.state['restarts'] = {'codex': list(self.SPENT)}
        self.exit()
        self.run_until(5)
        self.assertEqual(1, len(self.exit_mail()))
        self.assertIn('codex', self.r.state['exitGaveUp'])

    def no_pin(self):
        del self.session['restoreCommands'][IMPLEMENTER]
        self.exit()
        self.run_until(5)
        self.notify.assert_called_once()
        self.assertIn('codex', self.r.state['exitNoPin'])

    def human_uses_the_shell(self, start):
        """A command typed (two not-exited reads end the episode), then its output and a bare prompt."""
        self.exit(text=at_prompt() + 'git status')
        self.run_until(start + 0.5, start=start)
        self.assertNotIn('codex', self.r.exits.episodes)
        self.exit(text=at_prompt('On branch issue-7-fix\nnothing to commit, working tree clean'))
        self.run_until(70, start=start + 1)

    def assert_left_alone(self):
        self.assertEqual([], self.restarts())
        self.assertEqual(1, len(self.exit_mail()))
        self.notify.assert_called_once()

    def test_a_command_in_the_shell_after_giving_up_rearms_nothing(self):
        self.give_up()
        self.human_uses_the_shell(6)
        self.assert_left_alone()
        self.status.assert_called_once()
        self.assertTrue(self.logged('not restarting codex (kimi): exitGaveUp is set in relay.json'))

    def test_a_new_relay_after_giving_up_rearms_nothing(self):
        self.give_up()
        self.r = self.make_relay()
        self.run_until(70, start=6)
        self.assert_left_alone()
        self.assertEqual(1, len(self.logged('exitGaveUp is set in relay.json')))

    def test_a_crashed_agents_busy_row_does_not_clear_it(self):
        self.give_up()
        crashed = at_prompt(fixture('kimi/running-tool.txt'))
        self.assertTrue(peerchat.is_busy(crashed))
        self.exit(text=crashed)
        self.run_until(70, start=6)
        self.assert_left_alone()

    def test_an_agent_seen_twice_clears_it(self):
        self.give_up()
        self.agent(IMPLEMENTER, KIMI_IDLE)                           # one read: still latched
        self.tick(5.5)
        self.assertIn('codex', self.r.state['exitGaveUp'])
        self.exit()
        self.run_until(10, start=6)
        self.agent(IMPLEMENTER, KIMI_IDLE)                           # two reads: cleared, also on disk
        self.run_until(11, start=10.5)
        self.assertEqual({}, self.r.state['exitGaveUp'])
        saved = json.loads((self.hub_dir / 'state' / 'relay.json').read_text(encoding='utf-8'))
        self.assertEqual({}, saved['exitGaveUp'])
        self.exit()                                                  # judged afresh: the hour is still spent
        self.run_until(15, start=11.5)
        self.assertEqual(2, len(self.exit_mail()))
        self.agent(IMPLEMENTER, KIMI_IDLE)
        self.run_until(61, start=15.5)
        self.exit()                                                  # past the hour: restarted
        self.run_until(64, start=61.5)
        self.assertEqual(1, len(self.restarts()))

    def test_a_command_in_the_shell_after_the_no_pin_alert_rearms_nothing(self):
        self.no_pin()
        self.human_uses_the_shell(6)
        self.notify.assert_called_once()
        self.assertEqual(1, len(self.exit_mail()))

    def test_a_new_relay_after_the_no_pin_alert_rearms_nothing(self):
        self.no_pin()
        self.r = self.make_relay()
        self.run_until(30, start=6)
        self.notify.assert_called_once()
        self.assertEqual(1, len(self.exit_mail()))

    def test_an_agent_seen_twice_clears_the_no_pin_alert(self):
        self.no_pin()
        self.agent(IMPLEMENTER, KIMI_IDLE)
        self.run_until(6, start=5.5)
        self.assertEqual({}, self.r.state['exitNoPin'])
        self.exit()
        self.run_until(10, start=6.5)
        self.assertEqual(2, self.notify.call_count)
        self.assertEqual([], self.typed.call_args_list)

    def test_dry_run_reads_the_latch_and_writes_none(self):
        self.r.state['exitGaveUp'] = {'codex': 1_000_000.0}
        self.r._save()
        self.r = self.make_relay(dry_run=True)
        self.r.state.pop('exitGaveUp')
        self.r.state['restarts'] = {'codex': list(self.SPENT)}
        self.exit()
        self.run_until(5)
        self.assertNotIn('exitGaveUp', self.r.state)
        self.assertEqual([], self.exit_mail())

    def test_dry_run_never_clears_the_latch(self):
        # FIX r3 M1: a dry run that sees the agent back must not write its stale state over relay.json.
        self.r.state['exitGaveUp'] = {'codex': 1_000_000.0}
        self.r._save()
        path = self.hub_dir / 'state' / 'relay.json'
        before = path.read_bytes()
        self.r = self.make_relay(dry_run=True)
        self.agent(IMPLEMENTER, KIMI_IDLE)
        self.run_until(3)
        self.assertEqual(before, path.read_bytes())
        self.assertIn('codex', self.r.state['exitGaveUp'])
        self.assertEqual(1, len(self.logged('[dry-run] would clear exitGaveUp for codex')))

    def test_dry_run_logs_each_box_once(self):
        # FIX r4 m1: two latched boxes alternated in the watch-wide note and re-logged every tick.
        self.r.state['exitGaveUp'] = {'codex': 1_000_000.0}
        self.r.state['exitNoPin'] = {'claude': 1_000_000.0}
        self.r._save()
        self.r = self.make_relay(dry_run=True)
        self.agent(IMPLEMENTER, KIMI_IDLE)
        self.agent(PLANNER, CLAUDE_IDLE)
        self.run_until(5)                                            # eleven reads
        self.assertEqual(1, len(self.logged('[dry-run] would clear exitGaveUp for codex')))
        self.assertEqual(1, len(self.logged('[dry-run] would clear exitNoPin for claude')))

    def assert_kept(self, box, pane, text):
        self.r.state['exitGaveUp'] = {box: 1_000_000.0}
        self.text[pane] = text
        self.session['foregroundShells'][self.session['paneIds'].index(pane)] = 'pwsh'
        self.run_until(2)                                            # five reads
        self.assertIn(box, self.r.state['exitGaveUp'])

    def test_a_command_below_a_crashed_claude_frame_does_not_clear_it(self):
        # FIX r3 M2: Claude's rules stay in the bottom rows above the command and its output.
        for name, below in (('pwsh', PS_COMMAND), ('bash', BASH_COMMAND), ('bash, bare', BASH_PROMPT)):
            for frame in (CLAUDE_IDLE, CLAUDE_RUNNING):
                with self.subTest(prompt=name, frame=frame[-40:]):
                    self.assertEqual('', peerchat.composer_content('claude', frame + below))
                    self.assert_kept('claude', PLANNER, frame + below)

    def test_a_command_below_a_crashed_kimi_or_codex_frame_does_not_clear_it(self):
        for name, below in (('pwsh', PS_COMMAND), ('bash', BASH_COMMAND), ('bash, bare', BASH_PROMPT)):
            with self.subTest(prompt=name):
                self.assert_kept('codex', IMPLEMENTER, KIMI_IDLE + below)
                self.assertFalse(relay.agent_in_view('codex', CODEX_IDLE + below))

    def test_a_pwsh_command_in_kimis_footer_rows_does_not_clear_it(self):
        # FIX r4 m2: within the rows kimi_box allows below the box, only SHELL_ROW_RE's pwsh branch sees the prompt.
        self.assertTrue(peerchat.composer_content('kimi', KIMI_PS_FOOTER) is not None)
        self.assertFalse(relay.agent_in_view('kimi', KIMI_PS_FOOTER))
        self.assert_kept('codex', IMPLEMENTER, KIMI_PS_FOOTER)

    def test_a_live_claude_frame_twice_clears_it(self):
        self.r.state['exitGaveUp'] = {'claude': 1_000_000.0}
        self.agent(PLANNER, CLAUDE_IDLE)
        self.tick(0)
        self.assertIn('claude', self.r.state['exitGaveUp'])
        self.tick(0.5)
        self.assertEqual({}, self.r.state['exitGaveUp'])


class AgentInView(unittest.TestCase):
    def test_live_frames(self):
        for tool, frame in (('claude', CLAUDE_IDLE), ('claude', CLAUDE_RUNNING), ('codex', CODEX_IDLE),
                            ('kimi', KIMI_IDLE), ('kimi', fixture('kimi/running-tool.txt'))):
            with self.subTest(tool=tool, frame=frame[-40:]):
                self.assertTrue(relay.agent_in_view(tool, frame))

    def test_a_shell_below_the_frame(self):
        for tool, frame in (('claude', CLAUDE_IDLE), ('codex', CODEX_IDLE), ('kimi', KIMI_IDLE)):
            for below in (PS_COMMAND, BASH_COMMAND, BASH_PROMPT, '\n' + PROMPT):
                with self.subTest(tool=tool, below=below[:30]):
                    self.assertFalse(relay.agent_in_view(tool, frame + below))


class Pointer(ExitFixture):
    def test_no_pointer_into_a_shell(self):
        # FIX r1 M3: idle_blockers calls a shell idle; the pointer still needs a composer.
        self.exit()
        self.run_until(2)
        self.exit(text=at_prompt(KIMI_IDLE), shell=None)              # not exited by the tree's account
        self.run_until(5, start=2.5)
        self.send.assert_not_called()
        self.agent(IMPLEMENTER, KIMI_IDLE)
        self.tick(5.5)
        self.send.assert_called_once()

    def test_pointer_deadline(self):
        self.exit()
        self.run_until(2)
        self.agent(IMPLEMENTER, fixture('kimi/running-tool.txt'))
        self.run_until(13, start=2.5)
        self.send.assert_not_called()
        self.assertEqual(1, len(self.logged('dropped the resume pointer for codex')))
        self.agent(IMPLEMENTER, KIMI_IDLE)
        self.run_until(15, start=13.5)
        self.send.assert_not_called()

    def test_a_refused_pointer_is_retried(self):
        self.exit()
        self.run_until(2)
        self.agent(IMPLEMENTER, KIMI_IDLE)
        self.send.side_effect = [peerchat.Refused('composer changed'), 'submitted']
        self.run_until(3, start=2.5)
        self.assertEqual(2, self.send.call_count)
        self.tick(3.5)
        self.assertEqual(2, self.send.call_count)


class IdleBlockers(unittest.TestCase):
    def test_a_shell_prompt_is_idle(self):
        # FIX r1 M3: no agent is left to be busy or to hold a draft, whatever frame the crash left above.
        for tool, frame in (('claude', at_prompt(CLAUDE_IDLE)), ('claude', at_prompt(CLAUDE_RUNNING)),
                            ('codex', at_prompt()), ('kimi', at_prompt(KIMI_IDLE)),
                            ('kimi', at_prompt(fixture('kimi/draft-wrapped.txt')))):
            with self.subTest(tool=tool, frame=frame[-80:]):
                self.assertEqual([], closer.idle_blockers(relay.Peer('codex', tool, IMPLEMENTER), frame))

    def test_an_idle_agent_is_still_idle(self):
        self.assertEqual([], closer.idle_blockers(relay.Peer('claude', 'claude', PLANNER), CLAUDE_IDLE))


if __name__ == '__main__':
    unittest.main()
