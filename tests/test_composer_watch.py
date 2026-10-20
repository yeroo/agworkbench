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
from types import SimpleNamespace
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
        self.r.composers.automatic = lambda peer: self.automatic
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
        self.assertTrue(any("the pane is not an automatic session's" in line for line in self.logs))
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
            # The watch's two reads and the clear's first see an idle pane; the clear's second
            # snapshot (composer_state's own read) sees a turn begin.
            self.busy = len(reads) > 3
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


class InterruptedClearRemainder(ComposerWatchFixture):
    def test_a_remainder_of_an_interrupted_clear_carries_the_mail_ids_into_the_clear_that_removes_it(self):
        self.typed = self.pointer + self.pointer
        state = {'fail_reads': 0}
        original_screen = self.screen

        def key(pane, text):
            self.keys.append(text)
            if text == peerchat.CLEAR_KEY:
                if self.typed and self.typed.startswith(self.pointer):
                    # Ctrl+U took the marker and left a prefix; the pane then went quiet for a look.
                    self.typed = 'Chat from Workbench: work'
                    state['fail_reads'] = 1
                else:
                    self.typed = None

        def screen(pane):
            if state['fail_reads']:
                state['fail_reads'] -= 1
                raise agw.CtlError('pane busy')
            return original_screen(pane)
        self.enterContext(patch.object(agw, 'pane_text', side_effect=screen))
        self.enterContext(patch.object(agw, 'press_key', side_effect=key))
        self.enterContext(patch.object(agw, 'type_into', side_effect=key))
        self.r.state['announced'] = ['m1']
        self.look(0)
        self.look(60)
        self.assertEqual([peerchat.CLEAR_KEY], self.keys)
        self.assertTrue(any('could not clear stalled composer text in codex' in line for line in self.logs))
        self.assertEqual(['m1'], self.r.state['announced'], 'nothing reported yet: the remainder is still there')
        # The remainder is new content: timed from its first look, cleared a minute later, and that
        # clear reports m1, once, so the mail rings again.
        self.look(61)
        self.look(120)
        self.assertEqual([peerchat.CLEAR_KEY], self.keys, 'the remainder is under a minute old')
        self.look(121)
        self.assertEqual([peerchat.CLEAR_KEY, peerchat.CLEAR_KEY], self.keys)
        self.assertIsNone(self.typed)
        self.assertEqual(1, len(self.cleared_lines()))
        self.assertIn('the remainder of an interrupted clear that carried m1', self.cleared_lines()[0])
        self.assertEqual([], self.r.state.get('announced', []), 'm1 rings again')
        self.assertEqual(0, self.notify.call_count, 'the relay\'s own text: no alert')

    def interrupted_clears(self, outcomes):
        """Each Ctrl+U leaves the next of `outcomes` in the composer (None: empty) and, unless it is
        None, the pane goes quiet for one look afterwards."""
        state = {'fail_reads': 0}
        original_screen = self.screen
        queue = list(outcomes)

        def key(pane, text):
            self.keys.append(text)
            if text == peerchat.CLEAR_KEY:
                self.typed = queue.pop(0)
                if self.typed is not None:
                    state['fail_reads'] = 1

        def screen(pane):
            if state['fail_reads']:
                state['fail_reads'] -= 1
                raise agw.CtlError('pane busy')
            return original_screen(pane)
        self.enterContext(patch.object(agw, 'pane_text', side_effect=screen))
        self.enterContext(patch.object(agw, 'press_key', side_effect=key))
        self.enterContext(patch.object(agw, 'type_into', side_effect=key))

    def test_a_remainder_that_still_shows_the_marker_reports_nothing_when_the_rescue_sends_it(self):
        # Ctrl+U took the second copy and left exactly the pointer: the rescue's to submit. Once it
        # has gone, the empty look reports nothing: m1 stays announced, its pointer did go.
        self.typed = self.pointer + self.pointer
        self.interrupted_clears([self.pointer])
        self.r.state['announced'] = ['m1']
        self.look(0)
        self.look(60)
        self.assertEqual([peerchat.CLEAR_KEY], self.keys)
        self.look(61)
        self.assertEqual({}, self.r.composers.pending, 'the marker is shown: the text reports itself')
        self.typed = None            # the rescue pressed the submit key
        self.look(62)
        self.assertEqual(['m1'], self.r.state['announced'], 'm1 was sent, not lost')
        self.assertEqual([], [line for line in self.logs if 'never went' in line])
        self.assertEqual([peerchat.CLEAR_KEY], self.keys)

    def test_a_second_interrupted_clear_that_changes_nothing_keeps_the_inherited_ids(self):
        self.typed = self.pointer + self.pointer
        remainder = 'Chat from Workbench: work'
        self.interrupted_clears([remainder, remainder, None])
        self.r.state['announced'] = ['m1']
        self.look(0)
        self.look(60)            # first clear: the marker goes, a prefix stays
        self.look(61)            # the remainder is seen
        self.look(121)           # second clear: nothing changes, the pane is quiet again
        self.assertEqual([peerchat.CLEAR_KEY] * 2, self.keys)
        self.assertEqual(['m1'], self.r.composers.pending['codex'][0], 'm1 is not in the text: still carried')
        self.look(122)           # third clear takes it
        self.assertEqual([peerchat.CLEAR_KEY] * 3, self.keys)
        self.assertIsNone(self.typed)
        self.assertEqual(1, len(self.cleared_lines()))
        self.assertIn('the remainder of an interrupted clear that carried m1', self.cleared_lines()[0])
        self.assertEqual([], self.r.state.get('announced', []), 'm1 rings again')

    def test_a_clear_that_runs_out_of_presses_keeps_the_ids_for_the_clear_that_finishes_it(self):
        # Three presses took the second copy and the marker, not the prefix: `clear_composer` gave
        # up, and the remainder carries m1 into the clear that removes it.
        self.typed = self.pointer + self.pointer
        remainder = 'Chat from Workbench: work'
        calls = []

        def clear_composer(pane, profile, content, idle=None, on_press=None):
            calls.append(content)
            for _ in range(peerchat.CLEAR_PRESSES if len(calls) == 1 else 1):
                self.keys.append(peerchat.CLEAR_KEY)
                on_press()
            self.typed = remainder if len(calls) == 1 else None
            return len(calls) > 1
        self.enterContext(patch.object(peerchat, 'clear_composer', side_effect=clear_composer))
        self.r.state['announced'] = ['m1']
        self.look(0)
        self.look(60)
        self.assertEqual([peerchat.CLEAR_KEY] * 3, self.keys)
        self.assertTrue(any('not empty after 3 presses' in line for line in self.logs))
        self.assertEqual((['m1'], peerchat.compact(self.pointer + self.pointer)), self.r.composers.pending['codex'])
        self.look(61)
        self.assertEqual(['m1'], self.r.composers.pending['codex'][0], 'the remainder shows no marker')
        self.look(121)
        self.assertEqual([peerchat.CLEAR_KEY] * 4, self.keys)
        self.assertIn('the remainder of an interrupted clear that carried m1', self.cleared_lines()[0])
        self.assertEqual([], self.r.state.get('announced', []), 'm1 rings again')


class WrappedIds(unittest.TestCase):
    def test_an_unmatched_id_opener_does_not_swallow_the_marker_after_it(self):
        # An accepted subject may contain `[id `; the whole marker after it is still the pointer's.
        text = 'Chat from Workbench: workbench mail from claude: handle [id parsing [id m1] - read it'
        self.assertEqual(['m1'], peerchat.pointer_ids(text))
        self.assertEqual('m1', peerchat.pointer_id(text))
        self.assertEqual(['m1', 'm2'], peerchat.pointer_ids('[id m1] then [id\n  m2]'))


class StalledTextMore(ComposerWatchFixture):
    def test_two_distinct_pointers_in_one_composer_are_both_announced_no_more(self):
        self.messages['m2'] = {'id': 'm2', 'from': 'claude', 'subject': 'second'}
        self.unread['codex'] = ['m1', 'm2']
        pointer2 = closer.relay_pointer(self.messages['m2'], self.r.agmsg, self.r.hub_dir)
        self.r.state['announced'] = ['m1', 'm2']
        self.typed = self.pointer + pointer2
        self.look(0)
        self.look(60)
        self.assertEqual([peerchat.CLEAR_KEY], self.keys)
        self.assertIn('(pointers for m1, m2)', self.cleared_lines()[0])
        self.assertEqual([], self.r.state['announced'])
        self.takes = True
        self.keys.clear()
        self.tick(61)
        self.assertEqual(['m1', 'm2'], self.r.state['announced'])

    def test_an_id_wrapped_across_rows_is_still_the_pointers(self):
        # Codex breaks a row before a letter that follows a hyphen; the joined rows read
        # "[id 2026…Z- claude-abcd]".
        mid = '20261020T120000Z-claude-abcd'
        self.messages[mid] = {'id': mid, 'from': 'claude', 'subject': 'review'}
        self.unread['codex'] = [mid]
        self.r.state['announced'] = [mid]
        pointer = closer.relay_pointer(self.messages[mid], self.r.agmsg, self.r.hub_dir)
        wrapped = pointer.replace('20261020T120000Z-claude-abcd]', '20261020T120000Z- claude-abcd]', 1)
        self.assertEqual(['20261020T120000Z-claude-abcd'], peerchat.pointer_ids(wrapped))
        self.typed = wrapped + wrapped
        self.look(0)
        self.look(60)
        self.assertEqual([peerchat.CLEAR_KEY], self.keys)
        self.assertEqual([], self.r.state['announced'], 'the wrapped id still names its mail')

    def test_an_interrupted_clear_is_forgotten_once_the_text_is_seen_again(self):
        self.r.state['announced'] = ['m1']
        self.typed = self.pointer + self.pointer
        self.look(0)

        def screen(pane):
            if peerchat.CLEAR_KEY in self.keys:
                raise agw.CtlError('pane unreadable for a moment')
            return ComposerWatchFixture.screen(self, pane)
        with patch.object(agw, 'pane_text', side_effect=screen):
            self.look(60)
        self.assertEqual([peerchat.CLEAR_KEY], self.keys)
        # The press did not take: the text is still there on the next look, so the interrupted
        # clear is forgotten and the clear that does take, on that look, reports it once.
        self.typed = self.pointer + self.pointer
        self.keys.clear()
        self.look(90)
        self.assertEqual([peerchat.CLEAR_KEY], self.keys)
        self.assertEqual({}, self.r.composers.pending)
        self.assertEqual([], self.r.state['announced'])
        self.assertEqual(1, len([line for line in self.logs if 'its pointer never went' in line]))
        self.look(150)
        self.assertEqual(1, len([line for line in self.logs if 'its pointer never went' in line]), 'reported once')

    def test_a_clear_interrupted_after_a_press_reports_on_the_next_empty_look(self):
        self.r.state['announced'] = ['m1']
        self.typed = self.pointer + self.pointer
        self.look(0)

        def screen(pane):
            if peerchat.CLEAR_KEY in self.keys:
                raise agw.CtlError('pane unreadable for a moment')
            return ComposerWatchFixture.screen(self, pane)
        with patch.object(agw, 'pane_text', side_effect=screen):
            self.look(60)
        self.assertEqual([peerchat.CLEAR_KEY], self.keys, 'one press went in before the pane stopped answering')
        self.assertTrue(any(line.startswith('could not clear stalled composer text in codex') for line in self.logs))
        self.assertEqual(['m1'], self.r.state['announced'], 'not yet: the clear is not known to have taken')
        # The next look finds the composer empty: the pointer's mail is announced no more.
        self.look(90)
        self.assertIn('codex composer is empty after an interrupted clear', self.logs)
        self.assertEqual([], self.r.state['announced'])
        self.assertEqual({}, self.r.composers.pending)

    def test_an_adopted_claude_pane_of_an_autonomous_loop_is_the_humans(self):
        folder = Path(__file__).resolve().parent / ('adopted ' + os.urandom(4).hex())
        (folder / 'state').mkdir(parents=True)
        self.addCleanup(shutil.rmtree, folder)
        (folder / 'state' / 'implementer.json').write_text(json.dumps({'autonomous': True}), encoding='utf-8')
        (folder / 'state' / 'claude.json').write_text(json.dumps({'origin': 'adopted', 'sessionId': 'x'}), encoding='utf-8')
        claude_peer = relay.Peer('claude', 'claude', 'claude-pane')
        codex_peer = relay.Peer('codex', 'codex', 'codex-pane')
        claude_implementer = relay.Peer('codex', 'claude', 'impl-pane')
        self.assertFalse(closer.automatic_pane(folder, claude_peer), "the caller's own Claude")
        self.assertTrue(closer.automatic_pane(folder, codex_peer), 'the Codex pane the workbench launched')
        self.assertTrue(closer.automatic_pane(folder, claude_implementer),
                        'a Claude implementer is the workbench\'s: the adoption is the planner pane\'s')
        (folder / 'state' / 'claude.json').write_text(json.dumps({'origin': 'fresh'}), encoding='utf-8')
        self.assertTrue(closer.automatic_pane(folder, claude_peer))
        (folder / 'state' / 'implementer.json').write_text(json.dumps({'autonomous': False}), encoding='utf-8')
        self.assertFalse(closer.automatic_pane(folder, codex_peer), 'not autonomous, not a member')
        (folder / 'state' / 'queue-member.json').write_text('{}', encoding='utf-8')
        self.assertTrue(closer.automatic_pane(folder, claude_peer), "a queue member's panes are the workbench's")


class Settling(ComposerWatchFixture):
    def test_the_settle_check_reads_once_per_peer_per_tick(self):
        self.messages['m2'] = {'id': 'm2', 'from': 'claude', 'subject': 'second'}
        self.messages['m3'] = {'id': 'm3', 'from': 'claude', 'subject': 'third'}
        self.unread['codex'] = ['m1', 'm2', 'm3']
        self.tick(0)                      # m1 fails after typing; m2 and m3 wait
        self.typed = None
        reads = []

        def screen(pane):
            reads.append(pane)
            return ComposerWatchFixture.screen(self, pane)
        with patch.object(agw, 'pane_text', side_effect=screen):
            self.tick(5)
        self.assertEqual(2, len(reads), 'one settle check (two snapshots) for the whole box')
        for mid in ('m1', 'm2', 'm3'):
            self.assertIn('composer read empty for', self.r.holds[('codex', mid)].reason)

    def test_a_greyed_suggestion_settles_and_the_precheck_names_it(self):
        self.tick(0)                      # the ring fails after typing
        self.typed = None
        with patch.object(peerchat, 'composer_state', return_value=('run a third round', 'ambiguous', 'frame')):
            self.tick(5)
        self.assertIn('codex composer settled; rings resume', self.logs)
        self.assertIn('ambiguous', self.r.holds[('codex', 'm1')].reason)


class Transcript(unittest.TestCase):
    def test_the_composer_block_is_found_as_trailing_block_finds_it(self):
        pointer = 'Chat from Workbench: workbench mail from claude: FIX r1 [id 20261007T163143Z-claude-ceba] - read it'
        # The status row plus the right-aligned footer, as Codex 0.160 draws a draft.
        frame = "\n".join([
            "• earlier cell",
            "─" * 50,
            "› Chat from Workbench: workbench mail from claude: FIX r1 [id",
            "  20261007T163143Z-claude-ceba] - read it",
            " ",
            "  GPT-6-Luna medium · ~/source/workbench/docxy-issue-976 · plan",
            "                                                      ⚠ 1 warning · f2 to view",
        ]) + "\n"
        self.assertEqual("• earlier cell", peerchat.transcript_rows('codex', frame))
        self.assertEqual("• earlier cell", peerchat.transcript_rows("codex", frame[:-1]))
        self.assertFalse(peerchat.echoed('codex', frame, pointer), "the pointer is the composer's, not an echo")
        # Trailing blank rows under the footer do not leak composer rows into the transcript.
        from frames import CODEX_UNSUBMITTED
        for blanks in (0, 2, 3):
            rows = peerchat.transcript_rows('codex', CODEX_UNSUBMITTED + "\n" * blanks)
            self.assertNotIn('Chat from Workbench', rows, f'{blanks} blank rows')
        # A two-row echo cell glued to the rule is transcript.
        echo = "\n".join([
            "• Chat from Workbench: workbench mail from claude: FIX r1 [id",
            "  20261007T163143Z-claude-ceba] - read it",
            "─" * 50,
            "› Ask Codex to do anything",
            "  gpt-6-astra high · fixture",
        ]) + "\n"
        self.assertIn('claude-ceba', peerchat.transcript_rows('codex', echo))
        self.assertTrue(peerchat.echoed('codex', echo, pointer))

    def test_transcript_rows_drop_the_composer_region(self):
        from frames import CLAUDE_IDLE, claude
        self.assertNotIn('> draft', peerchat.transcript_rows('claude', claude('draft')))
        self.assertIn('Brewed', peerchat.transcript_rows('claude', CLAUDE_IDLE))
        frame = codex('draft text', '• earlier line')
        rows = peerchat.transcript_rows('codex', frame)
        self.assertIn('earlier line', rows)
        self.assertNotIn('draft text', rows)

    def test_a_stale_echo_is_no_evidence(self):
        pointer = peerchat.compose_text('Chat from Workbench: ', 'review [id m1] - read it')
        before = codex('Ask Codex to do anything', '• ' + pointer)
        self.assertTrue(peerchat.echoed('codex', before, pointer))
        self.assertFalse(peerchat.echoed('codex', before, pointer, before), 'already there before typing')
        probe = peerchat.compose_text('Chat from Workbench: ', relay.PROBE_TEXT)
        old = codex('Ask Codex to do anything', '• ' + probe)
        self.assertFalse(peerchat.echoed('codex', old, probe, old), 'an hour-old probe is not this one')
        self.assertFalse(peerchat.echoed('codex', codex(pointer), pointer), 'text still in the composer rows is no echo')


class Retries(unittest.TestCase):
    def test_a_retry_key_gets_its_own_echo_window(self):
        # The composer reads empty while the typed line is undrawn, then shows the draft; the retry
        # key takes it and the echo draws within the window: no 'not confirmed' note.
        notes = []
        clock = Clock()
        pointer = peerchat.compose_text('Chat from Workbench: ', 'review [id m1] - read it')
        frames = [codex('Ask Codex to do anything')] * 9 + [codex(pointer)] * 30
        echoed = codex('Ask Codex to do anything', '• ' + pointer)
        state = {'keys': 0, 'after': 0}

        def pane_text(pane):
            if state['keys']:
                # After the retry key: the composer reads empty with no echo for four looks (the
                # earlier empty window, from the first frames, expired long ago), then the echo.
                state['after'] += 1
                return echoed if state['after'] > 4 else codex('Ask Codex to do anything')
            return frames.pop(0) if len(frames) > 1 else frames[0]

        def type_into(pane, text):
            state['keys'] += 1
        with patch.object(peerchat, 'NOTE', notes.append), patch.object(peerchat, 'now', clock.now), \
                patch.object(peerchat, 'pause', clock.pause), patch.object(agw, 'pane_text', side_effect=pane_text), \
                patch.object(agw, 'type_into', side_effect=type_into), \
                patch.object(agw, 'cursor_column', side_effect=agw.CtlError('no cursor')):
            outcome = peerchat.verify_submitted('pane', peerchat.PROFILES['codex'], pointer)
        self.assertTrue(outcome.startswith('submitted after retry'), outcome)
        self.assertEqual([], [n for n in notes if 'not confirmed' in n])
        self.assertGreaterEqual(state['after'], 5, 'verification waited for the echo after the retry')

    def test_each_extra_submit_key_is_noted(self):
        notes = []
        clock = Clock()
        pointer = peerchat.compose_text('Chat from Workbench: ', 'review [id m1] - read it')
        with patch.object(peerchat, 'NOTE', notes.append), patch.object(peerchat, 'now', clock.now), \
                patch.object(peerchat, 'pause', clock.pause), patch.object(agw, 'pane_text', return_value=codex(pointer)), \
                patch.object(agw, 'type_into'), patch.object(agw, 'cursor_column', side_effect=agw.CtlError('no cursor')):
            with self.assertRaises(peerchat.Failed):
                peerchat.verify_submitted('pane', peerchat.PROFILES['codex'], pointer)
        retries = [n for n in notes if n.startswith('submit retry')]
        self.assertEqual(peerchat.SUBMIT_RETRIES, len(retries))
        self.assertIn('the composer still shows', retries[0])
        self.assertTrue(any(n.startswith('submit Return fallback') for n in notes))


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
        close = closer.Closer(folder, 'o/r', 5, [peer], log=logs.append, clock=clock.now, dry_run=False,
                              composer_stall_seconds=30.0)
        close.unread_message = lambda box, mid: None
        close.needs_no_reading = lambda number, peer, message: False
        with patch.object(agw, 'pane_text', side_effect=screen), patch.object(agw, 'press_key', side_effect=key), \
                patch.object(agw, 'type_into', side_effect=key), patch.object(agw, 'set_status'), \
                patch.object(agw, 'notify'), patch.object(agw, 'cursor_column', side_effect=agw.CtlError('no cursor')), \
                patch.object(peerchat, 'now', clock.now), patch.object(peerchat, 'pause', clock.pause):
            close.watch_composers()
            clock.pause(30)
            cleared = close.watch_composers()
        self.assertEqual([peerchat.CLEAR_KEY], keys, "the queue's composerStallSeconds, 30, applied")
        self.assertEqual([(peer, None)], cleared)
        self.assertTrue(any('cleared stalled composer text in codex' in line for line in logs))
        self.assertTrue(any('ALERT codex composer: composer held text' in line for line in logs),
                        'no mail is waiting: the alert says what happened')

    def backstop(self, typed_text, *, attempts, no_reading):
        """Two looks 30 s apart at a codex composer holding `typed_text` under the backstop, with the
        rescue's attempts at m1 and `needs_no_reading` answering `no_reading`: (keys, cleared, logs)."""
        peer = relay.Peer('codex', 'codex', 'codex-pane')
        folder = Path(__file__).resolve().parent / ('closer composer ' + os.urandom(4).hex())
        (folder / 'state').mkdir(parents=True)
        (folder / 'state' / 'queue-member.json').write_text('{}', encoding='utf-8')
        self.addCleanup(shutil.rmtree, folder)
        message = {'id': 'm1', 'from': 'claude', 'subject': 'review'}
        pointer = closer.relay_pointer(message, closer.AGMSG, folder)
        logs = []
        clock = Clock()
        keys = []
        typed = [typed_text.replace('POINTER', pointer)]

        def screen(pane):
            return codex(typed[0] or 'Ask Codex to do anything')

        def key(pane, text):
            keys.append(text)
            if text == peerchat.CLEAR_KEY:
                typed[0] = None
        close = closer.Closer(folder, 'o/r', 5, [peer], log=logs.append, clock=clock.now, dry_run=False,
                              composer_stall_seconds=30.0)
        close.unread_message = lambda box, mid: message if mid == 'm1' else None
        close.needs_no_reading = lambda number, peer, message: no_reading
        close.rescue = SimpleNamespace(attempts={('codex', 'm1'): attempts})
        with patch.object(agw, 'pane_text', side_effect=screen), patch.object(agw, 'press_key', side_effect=key), \
                patch.object(agw, 'type_into', side_effect=key), patch.object(agw, 'set_status'), \
                patch.object(agw, 'notify'), patch.object(agw, 'cursor_column', side_effect=agw.CtlError('no cursor')), \
                patch.object(peerchat, 'now', clock.now), patch.object(peerchat, 'pause', clock.pause):
            close.watch_composers(5)
            clock.pause(30)
            cleared = close.watch_composers(5)
            clock.pause(30)
            cleared += close.watch_composers(5)
        return keys, [(p.box, mid) for p, mid in cleared], logs

    def test_the_backstop_leaves_an_exhausted_pointer_for_mail_the_close_waits_for(self):
        keys, cleared, logs = self.backstop('POINTER', attempts=closer.RESCUE_ATTEMPTS, no_reading=False)
        self.assertEqual([], keys, 'the pointer stays for the rescue and the human')
        self.assertEqual([], cleared)
        self.assertEqual(1, len([line for line in logs if 'which the close waits for; left' in line]), 'logged once')
        self.assertEqual([], [line for line in logs if 'cleared stalled composer text' in line])

    def test_the_backstop_clears_an_exhausted_pointer_for_mail_the_close_does_not_wait_for(self):
        keys, cleared, logs = self.backstop('POINTER', attempts=closer.RESCUE_ATTEMPTS, no_reading=True)
        self.assertEqual([peerchat.CLEAR_KEY], keys)
        self.assertEqual([('codex', 'm1')], cleared)
        self.assertTrue(any('which the rescue gave up on' in line for line in logs))

    def test_the_backstop_leaves_a_duplicated_pointer_for_mail_the_close_waits_for(self):
        # Two copies own nothing, so this is not the rescue's exact pointer; the mail still holds the
        # close, so the text stays for a human to submit.
        keys, cleared, logs = self.backstop('POINTERPOINTER', attempts=0, no_reading=False)
        self.assertEqual([], keys)
        self.assertEqual([], cleared)
        self.assertTrue(any('which the close waits for; left' in line for line in logs))

    def test_the_conductor_reads_the_queues_composer_stall_seconds(self):
        import conductor
        folder = Path(__file__).resolve().parent / ('conductor setting ' + os.urandom(4).hex())
        folder.mkdir()
        self.addCleanup(shutil.rmtree, folder)
        path = folder / 'config.json'
        self.assertIsNone(conductor.composer_stall_setting(str(path)), 'no file: the default')
        path.write_text(json.dumps({'composerStallSeconds': 0}), encoding='utf-8')
        self.assertEqual(0.0, conductor.composer_stall_setting(str(path)))
        path.write_text(json.dumps({'composerStallSeconds': 'soon'}), encoding='utf-8')
        self.assertIsNone(conductor.composer_stall_setting(str(path)))
        self.assertIsNone(conductor.composer_stall_setting(None))
        peer = relay.Peer('codex', 'codex', 'codex-pane')
        off = closer.Closer(folder, 'o/r', 5, [peer], log=lambda t: None, dry_run=False, composer_stall_seconds=0.0)
        with patch.object(agw, 'pane_text', return_value=codex('stuck')), patch.object(agw, 'press_key') as press, \
                patch.object(agw, 'cursor_column', side_effect=agw.CtlError('no cursor')):
            off.watch_composers()
            off.watch_composers()
        press.assert_not_called()


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
