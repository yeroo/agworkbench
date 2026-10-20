"""The composer watch (#130): text that sits typed-but-unsent in an agent's composer is the relay's.

No terminal and no network: the pane is a scripted frame, keys are recorded, the clocks are ours.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import relay  # noqa: E402
import closer  # noqa: E402
import agw  # noqa: E402
import peerchat  # noqa: E402
from frames import Clock, codex  # noqa: E402
from test_relay import DeliveryFixture  # noqa: E402

BUSY = '◦ Working (11s • esc to interrupt)'


class ComposerWatchFixture(DeliveryFixture):
    rescue_live = True       # the rescue is not patched away: it reads the same fake screen

    def setUp(self):
        super().setUp()
        self.pointer = closer.relay_pointer(self.messages['m1'], self.r.agmsg, self.r.hub_dir)
        self.clock = Clock()
        self.keys = []
        self.typed = None            # composer content, None for an empty composer
        self.busy = False            # a turn running in the pane
        self.takes = False           # does the pane take a submit key?
        self.automatic = True        # a queue member or an autonomous loop
        self.send.side_effect = self.real_send
        self.r.composers.automatic = lambda: self.automatic
        self.enterContext(patch.object(agw, 'pane_text', side_effect=self.screen))
        self.enterContext(patch.object(agw, 'type_into', side_effect=self.key))
        self.enterContext(patch.object(agw, 'press_key', side_effect=self.key))
        self.enterContext(patch.object(agw, 'cursor_column', side_effect=agw.CtlError('no cursor in fixture')))
        self.enterContext(patch.object(peerchat, 'now', self.clock.now))
        self.enterContext(patch.object(peerchat, 'pause', self.clock.pause))

    def screen(self, pane):
        content = self.typed if self.typed else 'Ask Codex to do anything'
        return codex(content, BUSY if self.busy else '')

    def key(self, pane, text):
        self.keys.append(text)
        if text == peerchat.CLEAR_KEY:
            self.typed = None
        elif text in ('\t', '\n'):
            if self.takes:
                self.typed = None
        else:
            self.typed = (self.typed or '') + text

    def look(self, instant):
        self.t = instant
        self.r.watch_composers()

    def cleared_lines(self):
        return [line for line in self.logs if line.startswith('cleared stalled composer text in codex')]


class StalledText(ComposerWatchFixture):
    def test_two_copies_of_a_pointer_are_cleared_after_a_minute_and_the_mail_rings_again(self):
        # The ring typed the pointer over one the parser missed: two copies, which `owns` nothing.
        self.typed = self.pointer + self.pointer
        self.look(0)
        self.look(59)
        self.assertEqual([], self.keys, 'under a minute: watched, not touched')
        self.look(60)
        self.assertEqual([peerchat.CLEAR_KEY], self.keys)
        self.assertIsNone(self.typed)
        self.assertEqual(1, len(self.cleared_lines()))
        self.assertIn('(a pointer for m1)', self.cleared_lines()[0])
        self.assertEqual(0, self.notify.call_count, 'a pointer of ours is no news to the human')
        # The mail is still unread: the next tick rings it into the empty composer.
        self.takes = True
        self.keys.clear()
        self.tick(61)
        self.assertEqual([self.pointer, '\t'], self.keys)
        self.assertIn('m1', self.r.state['announced'])

    def test_a_cleared_pointer_for_announced_mail_is_announced_no_more_and_rings_again(self):
        # The wrong-`submitted` case: the ring was recorded, the pointer sat there and got a twin.
        self.r.state['announced'] = ['m1']
        self.typed = self.pointer + self.pointer
        self.look(0)
        self.look(60)
        self.assertEqual([peerchat.CLEAR_KEY], self.keys)
        self.assertEqual([], self.r.state['announced'])
        self.assertIn('m1 was recorded as announced but its pointer never went; it rings again', self.logs)
        self.takes = True
        self.keys.clear()
        self.tick(61)
        self.assertEqual([self.pointer, '\t'], self.keys)
        self.assertEqual(['m1'], self.r.state['announced'])

    def test_exactly_the_pointer_for_unread_mail_is_left_to_the_rescue(self):
        self.typed = self.pointer
        for instant in (0, 60, 120):
            self.look(instant)
        self.assertEqual([], self.keys)
        self.assertTrue(self.r.composers.stalled('codex'))
        self.assertTrue(any(line.startswith('composer text stalled in codex after 60s') for line in self.logs))

    def test_the_pointer_the_rescue_gave_up_on_is_cleared_and_rings_again(self):
        self.r.state['announced'] = ['m1']
        self.typed = self.pointer
        # The rescue's looks: remembered, then three submit keys the pane never takes.
        for instant in (0, 30, 60, 90):
            self.t = instant
            self.r.rescue_pointers()
        self.assertEqual(['\t'] * 3, self.keys)
        self.assertTrue(any(line.startswith('UNSUBMITTED pointer for m1 in codex') for line in self.logs))
        self.keys.clear()
        self.look(91)                     # the watch's first look: remembered
        self.look(151)
        self.assertEqual([peerchat.CLEAR_KEY], self.keys)
        self.assertIn('which the rescue gave up on', self.cleared_lines()[0])
        self.assertEqual([], self.r.state['announced'])
        self.takes = True
        self.keys.clear()
        self.tick(92)
        self.assertEqual([self.pointer, '\t'], self.keys)

    def test_a_pointer_for_mail_already_read_is_cleared(self):
        self.typed = self.pointer
        self.unread['codex'] = []
        self.look(0)
        self.look(60)
        self.assertEqual([peerchat.CLEAR_KEY], self.keys)
        self.assertIn('(a pointer for m1, whose mail is not unread)', self.cleared_lines()[0])

    def test_text_nobody_typed_is_cleared_and_alerted_once(self):
        self.typed = 'git push --force origin main'
        self.look(0)
        self.look(60)
        self.assertEqual([peerchat.CLEAR_KEY], self.keys)
        self.assertIn('(text that is not a relay pointer)', self.cleared_lines()[0])
        self.assertEqual(1, self.notify.call_count)
        message = self.notify.call_args.args[1]
        self.assertIn('no ring of the relay explains', message)
        self.assertIn('git push --force', message)
        self.status.assert_called_with('blocked', sound=True, blink=True, pane_id='codex-pane')
        # The same text again is cleared again, but the human hears of it once.
        self.typed = 'git push --force origin main'
        self.look(120)
        self.look(180)
        self.assertEqual([peerchat.CLEAR_KEY] * 2, self.keys)
        self.assertEqual(1, self.notify.call_count)

    def test_the_relays_own_probe_text_is_cleared_without_an_alert(self):
        self.typed = peerchat.compose_text('Chat from Workbench: ', relay.PROBE_TEXT)
        self.look(0)
        self.look(60)
        self.assertEqual([peerchat.CLEAR_KEY], self.keys)
        self.assertIn("(the relay's own text)", self.cleared_lines()[0])
        self.assertEqual(0, self.notify.call_count)

    def test_an_adopted_session_keeps_a_humans_draft_but_loses_the_relays_text(self):
        self.automatic = False
        self.typed = 'my half-written question'
        self.look(0)
        self.look(60)
        self.look(120)
        self.assertEqual([], self.keys, 'a human may be drafting here')
        self.assertEqual(0, self.notify.call_count)
        self.assertEqual(1, len([line for line in self.logs if line.startswith('stalled text in codex is not the relay')]))
        self.assertTrue(self.r.composers.stalled('codex'), 'stalled all the same, for the stall watch')
        self.typed = self.pointer + self.pointer
        self.look(180)
        self.look(240)
        self.assertEqual([peerchat.CLEAR_KEY], self.keys, 'the relay typed this: it goes')

    def test_a_busy_pane_waits_for_its_turn_to_end(self):
        self.typed = 'half a thought'
        self.busy = True
        self.look(0)
        self.look(90)
        self.assertEqual([], self.keys, 'never a key into a running turn')
        self.assertTrue(self.r.composers.stalled('codex'))
        self.assertTrue(any(line.startswith('composer text stalled in codex after 90s') for line in self.logs))
        self.busy = False
        self.look(120)
        self.assertEqual([peerchat.CLEAR_KEY], self.keys)

    def test_a_turn_that_starts_under_the_clear_stops_it(self):
        self.typed = 'half a thought'
        self.look(0)
        reads = []

        def screen(pane):
            reads.append(pane)
            # The watch's two reads see an idle pane; the clear's own read sees a turn begin.
            self.busy = len(reads) > 2
            return ComposerWatchFixture.screen(self, pane)
        with patch.object(agw, 'pane_text', side_effect=screen):
            self.look(60)
        self.assertEqual([], self.keys)
        self.assertTrue(any('a turn started in the target pane' in line for line in self.logs))

    def test_changed_text_restarts_the_clock(self):
        self.typed = 'first'
        self.look(0)
        self.typed = 'second'
        self.look(60)
        self.assertEqual([], self.keys)
        self.look(119)
        self.assertEqual([], self.keys)
        self.look(120)
        self.assertEqual([peerchat.CLEAR_KEY], self.keys)

    def test_an_empty_composer_is_never_touched(self):
        for instant in (0, 60, 120):
            self.look(instant)
        self.assertEqual([], self.keys)
        self.assertFalse(self.r.composers.stalled('codex'))

    def test_the_watch_is_off_at_zero(self):
        self.r.composers.stall_seconds = 0
        self.typed = 'left here'
        for instant in (0, 60, 600):
            self.look(instant)
        self.assertEqual([], self.keys)
        self.assertFalse(self.r.composers.stalled('codex'))

    def test_a_clear_that_does_not_empty_the_composer_is_logged(self):
        self.typed = 'stuck text'
        self.enterContext(patch.object(peerchat, 'clear_composer', return_value=False))
        self.look(0)
        self.look(60)
        self.assertTrue(any(line.startswith('could not clear stalled composer text in codex: not empty after')
                            for line in self.logs))
        self.assertTrue(self.r.composers.stalled('codex'), 'still stalled, tried again next look')


class StallWatchReadsIt(ComposerWatchFixture):
    def test_a_stalled_composer_no_longer_counts_as_busy(self):
        self.typed = 'stuck text'
        self.enterContext(patch.object(peerchat, 'clear_composer', return_value=False))
        texts = {'codex': self.screen('codex-pane')}
        self.look(0)
        self.assertIn('codex composer is not provably empty', self.r.stall.busy(texts))
        self.look(60)
        self.assertNotIn('codex composer is not provably empty', self.r.stall.busy(texts))
        self.assertEqual([], self.r.stall.busy(texts), 'idle: the stall clock may run')
        # The close keeps the reason: the text has to go before the pane may close.
        self.assertIn('codex composer is not provably empty', closer.idle_blockers(self.peer, texts['codex']))


class FailedRings(ComposerWatchFixture):
    def test_nothing_is_typed_over_a_failed_ring_until_the_composer_settles(self):
        # The ring types the pointer; the pane takes no submit key: Failed, the pointer sits there.
        self.tick(0)
        self.assertEqual([self.pointer, '\t', '\t', '\t', '\t', '\n'], self.keys)
        self.assertTrue(any(line.startswith('FAILED ringing codex for m1') for line in self.logs))
        self.keys.clear()
        # docxy-project-pro #12: the screen shows an empty composer although the text is there (a
        # slow redraw). The next ticks type nothing.
        self.typed = None
        self.tick(5)
        self.tick(30)
        self.assertEqual([], self.keys)
        self.assertIn('composer read empty for', self.r.holds[('codex', 'm1')].reason)
        # A minute of empty reads later the failed ring has settled and the mail rings.
        self.takes = True
        self.tick(66)
        self.assertEqual([self.pointer, '\t'], self.keys)
        self.assertIn('codex composer settled; rings resume', self.logs)
        self.assertIn('m1', self.r.state['announced'])

    def test_text_showing_up_again_restarts_the_settle(self):
        self.tick(0)
        self.keys.clear()
        self.typed = None
        self.tick(5)                      # empty: the settle clock starts
        self.typed = self.pointer         # the redraw catches up: the text is there after all
        self.tick(40)
        self.assertIn('still holds what a failed ring typed', self.r.holds[('codex', 'm1')].reason)
        self.typed = None
        self.tick(70)                     # empty again: a fresh minute
        self.tick(100)
        self.assertEqual([], self.keys)
        self.takes = True
        self.tick(131)
        self.assertEqual([self.pointer, '\t'], self.keys)

    def test_the_failed_rings_text_is_cleared_after_a_minute_and_rung_again(self):
        self.tick(0)
        self.assertEqual(self.pointer, self.typed)
        self.keys.clear()
        # Exactly the pointer for unread mail is the rescue's; a copy the TUI mangled is not, and goes
        # a minute after it was first seen. The clear settles the box at once.
        self.look(30)
        self.typed = self.pointer + ' and what the TUI did with it'
        self.look(60)
        self.look(119)
        self.assertEqual([], self.keys)
        self.look(120)
        self.assertEqual([peerchat.CLEAR_KEY], self.keys)
        self.assertIsNone(self.typed)
        self.takes = True
        self.keys.clear()
        self.tick(121)
        self.assertEqual([self.pointer, '\t'], self.keys)
        self.assertIn('m1', self.r.state['announced'])


class Echo(unittest.TestCase):
    def verify(self, frames, pointer):
        notes = []
        clock = Clock()
        queue = list(frames)

        def pane_text(pane):
            return queue.pop(0) if len(queue) > 1 else queue[0]
        with patch.object(peerchat, 'NOTE', notes.append), patch.object(peerchat, 'now', clock.now), \
                patch.object(peerchat, 'pause', clock.pause), patch.object(agw, 'pane_text', side_effect=pane_text), \
                patch.object(agw, 'type_into'), \
                patch.object(agw, 'cursor_column', side_effect=agw.CtlError('no cursor')):
            outcome = peerchat.verify_submitted('pane', peerchat.PROFILES['codex'], pointer)
        return outcome, notes

    def test_a_submit_without_an_echo_is_noted_and_counted(self):
        pointer = peerchat.compose_text('Chat from Workbench: ', 'review [id m1] - read it')
        outcome, notes = self.verify([codex('Ask Codex to do anything')], pointer)
        self.assertEqual('submitted', outcome)
        self.assertEqual(1, len(notes))
        self.assertIn('not confirmed by an echo', notes[0])

    def test_an_echoed_submit_is_silent(self):
        pointer = peerchat.compose_text('Chat from Workbench: ', 'review [id m1] - read it')
        outcome, notes = self.verify([codex('Ask Codex to do anything', '• ' + pointer)], pointer)
        self.assertEqual('submitted', outcome)
        self.assertEqual([], notes)


class CloserBackstop(unittest.TestCase):
    def test_the_conductors_close_backstop_clears_stalled_text(self):
        peer = relay.Peer('codex', 'codex', 'codex-pane')
        folder = Path(__file__).resolve().parent / ('closer composer ' + os.urandom(4).hex())
        (folder / 'state').mkdir(parents=True)
        (folder / 'state' / 'queue-member.json').write_text('{}', encoding='utf-8')
        self.addCleanup(shutil.rmtree, folder)
        logs = []
        clock = Clock()
        keys = []
        typed = ['stuck text']

        def screen(pane):
            return codex(typed[0] or 'Ask Codex to do anything')

        def key(pane, text):
            keys.append(text)
            if text == peerchat.CLEAR_KEY:
                typed[0] = None
        close = closer.Closer(folder, 'o/r', 5, [peer], log=logs.append, clock=clock.now, dry_run=False)
        close.unread_message = lambda box, mid: None
        with patch.object(agw, 'pane_text', side_effect=screen), patch.object(agw, 'press_key', side_effect=key), \
                patch.object(agw, 'type_into', side_effect=key), patch.object(agw, 'set_status'), \
                patch.object(agw, 'notify'), patch.object(agw, 'cursor_column', side_effect=agw.CtlError('no cursor')), \
                patch.object(peerchat, 'now', clock.now), patch.object(peerchat, 'pause', clock.pause):
            close.watch_composers()
            clock.pause(60)
            cleared = close.watch_composers()
        self.assertEqual([peerchat.CLEAR_KEY], keys)
        self.assertEqual([(peer, None)], cleared)
        self.assertTrue(any('cleared stalled composer text in codex' in line for line in logs))


class Setting(unittest.TestCase):
    def test_composer_stall_seconds_reads_the_config(self):
        folder = Path(__file__).resolve().parent / ('composer setting ' + os.urandom(4).hex())
        folder.mkdir()
        self.addCleanup(lambda: [p.unlink() for p in folder.iterdir()] and folder.rmdir())
        path = folder / 'config.json'
        with patch.dict(os.environ, {'AGWORKBENCH_CONFIG': str(path)}):
            self.assertEqual(60.0, relay.composer_stall_setting(), 'no file: the default')
            path.write_text(json.dumps({'composerStallSeconds': 90}), encoding='utf-8')
            self.assertEqual(90.0, relay.composer_stall_setting())
            path.write_text(json.dumps({'composerStallSeconds': 0}), encoding='utf-8')
            self.assertEqual(0.0, relay.composer_stall_setting(), 'zero turns the watch off')
            path.write_text(json.dumps({'composerStallSeconds': 'soon'}), encoding='utf-8')
            self.assertEqual(60.0, relay.composer_stall_setting(), 'not a number: the default')
            path.write_text(json.dumps({'composerStallSeconds': -1, 'stallMinutes': 7}), encoding='utf-8')
            self.assertEqual(60.0, relay.composer_stall_setting(), 'negative: the default')
            self.assertEqual(7.0, relay.stall_setting(), 'stallMinutes still reads')


if __name__ == '__main__':
    unittest.main()
