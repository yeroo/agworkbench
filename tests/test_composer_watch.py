"""The composer watch (#130): text that sits typed-but-unsent in an agent's composer is the relay's.

No terminal and no network: the pane is a scripted frame, keys are recorded, the clocks are ours.
"""

from __future__ import annotations

import json
import os
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
        self.send.side_effect = self.real_send
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

    def test_exactly_the_pointer_for_unread_mail_is_left_to_the_rescue(self):
        self.typed = self.pointer
        for instant in (0, 60, 120):
            self.look(instant)
        self.assertEqual([], self.keys)
        self.assertTrue(self.r.composers.stalled('codex'))
        self.assertTrue(any(line.startswith('composer text stalled in codex after 60s') for line in self.logs))

    def test_a_pointer_for_mail_already_read_is_cleared(self):
        self.typed = self.pointer
        self.unread['codex'] = []
        self.look(0)
        self.look(60)
        self.assertEqual([peerchat.CLEAR_KEY], self.keys)
        self.assertIn('(a pointer for m1)', self.cleared_lines()[0])

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


class FailedRings(ComposerWatchFixture):
    def test_the_failed_rings_text_is_cleared_after_a_minute_and_rung_again(self):
        # The ring types the pointer; the pane takes no submit key: Failed, the pointer sits there.
        self.tick(0)
        self.assertEqual(self.pointer, self.typed)
        self.assertTrue(any(line.startswith('FAILED ringing codex for m1') for line in self.logs))
        self.keys.clear()
        # Exactly the pointer for unread mail is the rescue's; a copy the TUI mangled is not, and goes
        # a minute after it was first seen.
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
        self.assertEqual([self.pointer, '	'], self.keys)
        self.assertIn('m1', self.r.state['announced'])


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
