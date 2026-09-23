"""The relay's decisions, with no terminal and no network.

The relay's effects (typing into panes, calling gh) are thin; what matters is WHEN it acts. These
tests pin that: which PR changes produce mail, that a re-read of the same PR produces none, and that
Claude is never rung while it is mid-turn.
"""

from __future__ import annotations

import sys
import os
import copy
import json
import shutil
import uuid
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import relay  # noqa: E402
import agw
import hub
import peerchat
from frames import CLAUDE_IDLE, CLAUDE_RUNNING, CODEX_IDLE, Clock, FakeAgw, codex

OPEN = {"number": 7, "url": "https://github.com/o/r/pull/7", "state": "OPEN", "reviewDecision": "",
        "reviews": [], "comments": [], "inline": []}


def with_(base: dict, **changes) -> dict:
    out = dict(base)
    out.update(changes)
    return out


class Busy(unittest.TestCase):
    def test_a_running_turn_is_busy(self):
        self.assertTrue(relay.is_busy("output\n\n✢ Working… (12s · esc to interrupt)\n\n>\n"))

    def test_an_idle_composer_is_not_busy(self):
        self.assertFalse(relay.is_busy("done.\n\n────────\n>\n────────\n  footer\n"))

    def test_only_the_bottom_of_the_pane_counts(self):
        # "esc to interrupt" scrolled far up in history is an old turn, not the current one.
        old = "Working (esc to interrupt)\n" + "line\n" * 40 + ">\n"
        self.assertFalse(relay.is_busy(old))


class PrEvents(unittest.TestCase):
    def test_nothing_before_a_pr_exists(self):
        self.assertEqual([], relay.pr_events(None, None))

    def test_a_new_pr_is_announced_once(self):
        events = relay.pr_events(None, OPEN)
        self.assertEqual(1, len(events))
        self.assertIn("PR #7 is open", events[0]["subject"])
        self.assertEqual([], relay.pr_events(OPEN, OPEN), "re-reading the same PR must stay silent")

    def test_a_new_review_is_reported_with_its_state_and_author(self):
        review = {"id": "R1", "state": "CHANGES_REQUESTED", "author": {"login": "yeroo"}, "body": "fix x"}
        events = relay.pr_events(OPEN, with_(OPEN, reviews=[review]))
        self.assertEqual(1, len(events))
        self.assertIn("yeroo", events[0]["subject"])
        self.assertIn("CHANGES_REQUESTED", events[0]["subject"])
        self.assertEqual("fix x", events[0]["body"])

    def test_a_line_comment_names_the_file_and_line(self):
        inline = {"id": 55, "user": {"login": "yeroo"}, "path": "app/x.py", "line": 12, "body": "why?"}
        events = relay.pr_events(OPEN, with_(OPEN, inline=[inline]))
        self.assertIn("app/x.py:12", events[0]["subject"])

    def test_a_conversation_comment_is_reported(self):
        comment = {"id": "C1", "author": {"login": "yeroo"}, "body": "looks good"}
        events = relay.pr_events(OPEN, with_(OPEN, comments=[comment]))
        self.assertEqual("looks good", events[0]["body"])

    def test_an_approval_decision_is_reported(self):
        events = relay.pr_events(OPEN, with_(OPEN, reviewDecision="APPROVED"))
        self.assertTrue(any("APPROVED" in e["subject"] for e in events))

    def test_merge_ends_the_loop(self):
        merged = with_(OPEN, state="MERGED", mergedAt="2026-09-22T10:00:00Z")
        events = relay.pr_events(OPEN, merged)
        self.assertTrue(any("MERGED" in e["subject"] for e in events))
        self.assertTrue(relay.finished(merged))

    def test_a_close_without_merge_also_ends_it_and_says_so(self):
        closed = with_(OPEN, state="CLOSED")
        events = relay.pr_events(OPEN, closed)
        self.assertTrue(any("CLOSED without merging" in e["subject"] for e in events))
        self.assertTrue(relay.finished(closed))

    def test_an_open_pr_is_not_finished(self):
        self.assertFalse(relay.finished(OPEN))
        self.assertFalse(relay.finished(None))

    def test_old_reviews_are_not_re_announced_when_a_new_one_lands(self):
        first = {"id": "R1", "state": "COMMENTED", "author": {"login": "a"}, "body": "one"}
        second = {"id": "R2", "state": "APPROVED", "author": {"login": "a"}, "body": "two"}
        events = relay.pr_events(with_(OPEN, reviews=[first]), with_(OPEN, reviews=[first, second]))
        self.assertEqual(["two"], [e["body"] for e in events if e["kind"] == "review"])


class Pointer(unittest.TestCase):
    def test_the_pointer_carries_the_id_and_the_read_command_but_never_the_body(self):
        message = {"id": "20260922T1-codex-ab12", "from": "codex", "subject": "IMPLEMENTED abc123",
                   "body": "a long body that must stay in the file"}
        text = relay.pointer_text(message, Path("C:/wb/lib/agmsg.py"), Path("C:/clone/.workbench"))
        self.assertIn("20260922T1-codex-ab12", text)
        self.assertIn("read 20260922T1-codex-ab12", text)
        self.assertNotIn("long body", text)

    def test_the_pointer_is_one_line(self):
        message = {"id": "x", "from": "github", "subject": "PR #7 is open"}
        self.assertNotIn(chr(10), relay.pointer_text(message, Path("a"), Path("b")))


class PaneIds(unittest.TestCase):
    GOOD = "461a2dd0-4f22-49c3-a724-90e3b2cde3db"

    def test_a_single_character_is_not_a_pane(self):
        # the exact value the first live run produced
        with self.assertRaises(SystemExit):
            relay.check_panes("4", self.GOOD)

    def test_the_two_agents_cannot_share_a_pane(self):
        with self.assertRaises(SystemExit):
            relay.check_panes(self.GOOD, self.GOOD)

    def test_two_distinct_pane_ids_pass(self):
        relay.check_panes(self.GOOD, "d387360b-a120-4e4b-b7a4-4db3171780ab")


class DeliveryFixture(unittest.TestCase):
    def setUp(self):
        self.peer = relay.Peer('codex', 'codex', 'codex-pane')
        # Run the constructor, replacing only mailbox storage boundaries. No real hub is touched.
        with patch.dict(os.environ), patch.object(hub, 'reload_paths'), \
                patch.object(relay.Relay, '_load', return_value={'announced': [], 'pr': None}):
            self.r = relay.Relay(Path('fixture-hub'), [self.peer], 'o/r', 'issue-6', 5, 60)
        self.messages = {'m1': {'id': 'm1', 'from': 'claude', 'subject': 'review'}}
        self.unread = {'codex': ['m1']}
        self.r.hub = SimpleNamespace(unread=lambda box: [Path(mid + '.md') for mid in self.unread.get(box, [])],
                                     parse_message=lambda path: self.messages[path.stem])
        self.r._save = Mock()
        self.logs = []
        self.r.log = self.logs.append
        self.t = 0
        self.enterContext(patch.object(relay, 'now', lambda: self.t))
        # An accidental real terminal request is a hard test failure.
        self.enterContext(patch.object(agw, 'request', side_effect=AssertionError('real terminal request')))
        self.status = self.enterContext(patch.object(agw, 'set_status'))
        self.notify = self.enterContext(patch.object(agw, 'notify'))
        self.pane = self.enterContext(patch.object(agw, 'pane_text', return_value=CLAUDE_IDLE))
        self.real_send = peerchat.send
        self.send = self.enterContext(patch.object(peerchat, 'send', return_value='submitted'))

    def tick(self, instant):
        self.t = instant
        self.r.deliver_mail()

    def assert_unannounced(self):
        self.assertEqual([], self.r.state['announced'])
        self.r._save.assert_not_called()
        self.assertFalse(any(line.startswith('rang ') for line in self.logs))


class Delivery(DeliveryFixture):
    def test_failed_ring_alerts_throttles_and_recovers(self):
        self.send.side_effect = peerchat.Failed("pointer still unsent in composer: 'the pointer'")
        self.tick(0)
        self.assert_unannounced()
        self.status.assert_called_once_with('blocked', sound=True, blink=True, pane_id=self.peer.pane)
        self.assertEqual(1, self.notify.call_count)
        notice = self.notify.call_args.args[1]
        for text in ['codex', 'm1', 'the pointer']:
            self.assertIn(text, notice)
        self.assertTrue(any(line.startswith('FAILED ringing codex for m1:') for line in self.logs))
        self.tick(299)
        self.assertEqual(1, self.notify.call_count)
        self.tick(300)
        self.assertEqual(2, self.notify.call_count)
        self.send.side_effect = None
        self.tick(310)
        self.assertEqual(['m1'], self.r.state['announced'])
        self.r._save.assert_called_once()
        self.assertIn('rang codex for m1 (review) [submitted] after holding 310s', self.logs)
        self.status.assert_called_with('idle', pane_id=self.peer.pane)
        self.assertEqual({}, self.r.holds)

    def test_draft_hold_alerts_after_a_minute_and_clears_on_success(self):
        self.send.side_effect = peerchat.Refused("composer holds a draft: 'check if codex replied'")
        self.tick(0)
        self.tick(30)
        self.notify.assert_not_called()
        self.status.assert_not_called()
        self.tick(61)
        self.assertEqual(1, self.notify.call_count)
        self.assertIn('check if codex replied', self.notify.call_args.args[1])
        self.send.side_effect = None
        self.tick(90)
        self.assertIn('rang codex for m1 (review) [submitted] after holding 90s', self.logs)
        self.status.assert_called_with('idle', pane_id=self.peer.pane)

    def test_mid_turn_and_changed_reason_share_the_original_hold_and_throttle(self):
        self.peer = relay.Peer('claude', 'claude', 'claude-pane')
        self.r.peers = [self.peer]
        self.unread = {'claude': ['m1']}
        self.pane.return_value = CLAUDE_RUNNING
        self.tick(0)
        self.tick(70)
        self.send.assert_not_called()
        self.assertEqual(1, self.notify.call_count)
        self.assertIn('mid-turn', self.notify.call_args.args[1])
        self.pane.return_value = CLAUDE_IDLE
        self.send.side_effect = peerchat.Refused('composer holds a draft')
        self.tick(80)
        self.tick(369)
        self.assertEqual(1, self.notify.call_count)
        self.tick(370)
        self.assertEqual(2, self.notify.call_count)
        self.assertIn('composer holds a draft', self.notify.call_args.args[1])
        self.assertEqual(0, self.r.holds[('claude', 'm1')].first_at)

    def test_prewrite_terminal_failures_also_alert_after_a_minute(self):
        self.send.side_effect = agw.CtlError('unreachable')
        self.tick(0)
        self.tick(70)
        self.assert_unannounced()
        self.assertEqual(1, self.notify.call_count)
        self.assertIn('terminal not reachable', self.notify.call_args.args[1])

    def test_failed_then_refused_has_one_alert_throttle(self):
        self.send.side_effect = peerchat.Failed('submit failed')
        self.tick(0)
        self.send.side_effect = peerchat.Refused('composer holds the pointer')
        self.tick(5)
        self.tick(299)
        self.assertEqual(1, self.notify.call_count)
        self.tick(300)
        self.assertEqual(2, self.notify.call_count)
        self.assert_unannounced()

    def test_independently_read_mail_clears_its_alert(self):
        self.send.side_effect = peerchat.Failed('submit failed')
        self.tick(0)
        self.unread['codex'] = []
        self.tick(5)
        self.assertEqual({}, self.r.holds)
        self.status.assert_called_with('idle', pane_id=self.peer.pane)
        self.assert_unannounced()

    def test_failed_idle_reset_is_retried_without_another_ring(self):
        self.send.side_effect = peerchat.Failed('submit failed')
        self.tick(0)
        self.send.side_effect = None
        self.status.side_effect = [agw.CtlError('reset unavailable'), None]
        self.tick(5)
        self.assertEqual(['m1'], self.r.state['announced'])
        self.assertTrue(self.r.holds[('codex', 'm1')].clear_pending)
        sends = self.send.call_count
        self.tick(10)
        self.assertEqual(sends, self.send.call_count)
        self.assertEqual({}, self.r.holds)
        self.assertEqual([call('idle', pane_id=self.peer.pane)] * 2, self.status.call_args_list[-2:])
        self.assertIn('cleared hold codex for m1; last reason: submit failed', self.logs)

    def test_independently_read_mail_keeps_retrying_failed_status_reset(self):
        self.send.side_effect = peerchat.Failed('submit failed')
        self.tick(0)
        self.unread['codex'] = []
        self.status.side_effect = [OSError('reset unavailable'), None]
        self.tick(5)
        self.assertTrue(self.r.holds[('codex', 'm1')].clear_pending)
        self.tick(10)
        self.assertEqual({}, self.r.holds)
        self.assertEqual(1, self.send.call_count)

    def test_other_alerted_message_prevents_early_status_clear(self):
        self.messages['m2'] = {'id': 'm2', 'from': 'claude', 'subject': 'second'}
        self.unread['codex'].append('m2')
        self.send.side_effect = peerchat.Failed('submit failed')
        self.tick(0)
        self.send.side_effect = ['submitted', peerchat.Refused('holds a draft')]
        self.tick(5)
        self.assertEqual(['m1'], self.r.state['announced'])
        self.assertNotIn(call('idle', pane_id=self.peer.pane), self.status.call_args_list)
        self.send.side_effect = None
        self.tick(10)
        self.status.assert_called_with('idle', pane_id=self.peer.pane)
        self.assertEqual(['m1', 'm2'], self.r.state['announced'])

    def test_unalerted_hold_does_not_change_agent_status_on_success(self):
        self.send.side_effect = peerchat.Refused('a draft')
        self.tick(0)
        self.send.side_effect = None
        self.tick(5)
        self.status.assert_not_called()
        self.assertEqual({}, self.r.holds)

    def test_alert_errors_are_independent_and_do_not_end_delivery(self):
        self.messages['m2'] = {'id': 'm2', 'subject': 'second'}
        self.unread['codex'].append('m2')
        self.send.side_effect = [peerchat.Failed('submit failed'), 'submitted']
        self.status.side_effect = agw.CtlError('status broken')
        self.notify.side_effect = agw.CtlError('notify broken')
        self.tick(0)
        self.notify.assert_called_once()
        self.assertEqual(['m2'], self.r.state['announced'])
        self.assertTrue(any('could not set blocked status' in line for line in self.logs))
        self.assertTrue(any('could not notify' in line for line in self.logs))
        self.assertIn(('codex', 'm1'), self.r.holds)

    def test_holds_are_keyed_by_recipient_and_message(self):
        self.r.peers.append(relay.Peer('claude', 'claude', 'claude-pane'))
        self.unread['claude'] = ['m1']
        self.send.side_effect = peerchat.Failed('submit failed')
        self.tick(0)
        self.assertEqual({('claude', 'm1'), ('codex', 'm1')}, set(self.r.holds))
        self.assertEqual(2, self.notify.call_count)

    def test_real_send_refuses_stuck_text_then_rerings_after_composer_empties(self):
        pointer = peerchat.compose_text('Chat from Workbench: ',
                                        relay.pointer_text(self.messages['m1'], self.r.agmsg, self.r.hub_dir))
        fake = FakeAgw(after=lambda f: codex(pointer, 'Working (esc to interrupt)'))
        clock = Clock()
        self.send.side_effect = self.real_send
        with patch.object(agw, 'pane_text', fake.pane_text), patch.object(agw, 'type_into', fake.type_into), \
                patch.object(peerchat, 'now', clock.now), patch.object(peerchat, 'pause', clock.pause):
            self.tick(0)
            self.assert_unannounced()
            self.assertEqual([pointer, '\t', '\t', '\t'], fake.keys)
            self.tick(5)
            self.assertEqual([pointer, '\t', '\t', '\t'], fake.keys)
            self.assertEqual(1, self.notify.call_count)
            fake.frames = [CODEX_IDLE, codex(pointer), CODEX_IDLE]
            self.tick(10)
        self.assertEqual([pointer, '\t', '\t', '\t', pointer, '\t'], fake.keys)
        self.assertEqual(['m1'], self.r.state['announced'])
        self.status.assert_called_with('idle', pane_id=self.peer.pane)

    def test_dry_run_does_not_send_or_raise_terminal_alerts(self):
        self.r.dry_run = True
        self.r.peers = [relay.Peer('claude', 'claude', 'claude-pane')]
        self.unread = {'claude': ['m1']}
        self.pane.return_value = CLAUDE_RUNNING
        self.tick(0)
        self.tick(70)
        self.send.assert_not_called()
        self.status.assert_not_called()
        self.notify.assert_not_called()

    def test_dry_run_leaves_disk_state_and_mail_untouched(self):
        folder = Path(__file__).resolve().parent.parent / ('test relay ' + uuid.uuid4().hex)
        folder.mkdir()
        self.addCleanup(shutil.rmtree, folder)
        self.r.state_file = folder / 'relay.json'
        self.r.state['pr'] = copy.deepcopy(OPEN)
        before = copy.deepcopy(self.r.state)
        self.r.state_file.write_text(json.dumps(before), encoding='utf-8')
        original_bytes = self.r.state_file.read_bytes()
        self.r._save = relay.Relay._save.__get__(self.r)
        self.r.hub.write_message = Mock(side_effect=AssertionError('dry run filed mail'))
        self.r.fetch_pr = Mock(return_value=with_(OPEN, state='MERGED', mergedAt='now'))
        self.r.dry_run = True
        self.tick(0)
        self.assertTrue(self.r.watch_pr())
        self.assertEqual(before, self.r.state)
        self.assertEqual(original_bytes, self.r.state_file.read_bytes())
        self.r.hub.write_message.assert_not_called()
        self.send.assert_not_called()


class FinalNotices(DeliveryFixture):
    def prepare_final(self, state='MERGED'):
        self.peer = relay.Peer('claude', 'claude', 'claude-pane')
        self.r.peers = [self.peer]
        self.unread = {'claude': []}
        self.messages = {}
        self.r.state = {'pr': copy.deepcopy(OPEN), 'announced': []}
        self.r.holds.clear()
        self.r.fetch_pr = Mock(return_value=with_(OPEN, state=state, mergedAt='now'))
        self.r.stop_file = SimpleNamespace(exists=lambda: False)

        def write(**message):
            mid = 'final-' + message['to']
            self.messages[mid] = dict(message, id=mid)
            self.unread[message['to']].append(mid)
            return Path(mid + '.md')

        self.r.hub.write_message = Mock(side_effect=write)
        self.enterContext(patch.object(relay, 'pause', self.advance))

    def advance(self, seconds):
        self.t += seconds

    def test_merged_and_closed_notices_wait_until_the_third_tick(self):
        for state in ['MERGED', 'CLOSED']:
            with self.subTest(state=state):
                self.prepare_final(state)
                self.r.peers.append(relay.Peer('codex', 'codex', 'codex-pane'))
                self.unread['codex'] = []
                self.pane.side_effect = [CLAUDE_RUNNING, CLAUDE_RUNNING, CLAUDE_IDLE]
                before = self.send.call_count
                self.assertEqual(0, self.r.run())
                self.assertEqual(before + 2, self.send.call_count)
                self.assertEqual(['final-claude', 'final-codex'], self.r.state['announced'])
                self.r.fetch_pr.assert_called_once()
                self.assertFalse(self.r.pending_terminal_mail())

    def test_final_notice_alerts_if_held_over_a_minute(self):
        self.prepare_final()
        self.pane.side_effect = lambda pane: CLAUDE_RUNNING if self.t < 70 else CLAUDE_IDLE
        self.assertEqual(0, self.r.run())
        self.assertEqual(1, self.notify.call_count)
        self.assertIn('mid-turn', self.notify.call_args.args[1])
        self.status.assert_called_with('idle', pane_id=self.peer.pane)
        self.assertEqual(['final-claude'], self.r.state['announced'])

    def test_drain_waits_for_a_failed_idle_reset_without_ringing_again(self):
        self.prepare_final()
        self.pane.side_effect = lambda pane: CLAUDE_RUNNING if self.t < 70 else CLAUDE_IDLE
        self.status.side_effect = [None, agw.CtlError('reset unavailable'), None]
        self.assertEqual(0, self.r.run())
        self.assertEqual(75, self.t)
        self.send.assert_called_once()
        self.assertEqual({}, self.r.holds)
        self.status.assert_called_with('idle', pane_id=self.peer.pane)

    def test_drain_ceiling_logs_the_remaining_recipient_id_and_reason(self):
        self.prepare_final()
        self.pane.return_value = CLAUDE_RUNNING
        self.assertEqual(0, self.r.run())
        self.assertEqual(relay.TERMINAL_DRAIN_TIMEOUT, self.t)
        self.assertEqual([], self.r.state['announced'])
        self.assertIn('drain deadline reached', self.logs[-1])
        self.assertIn('claude/final-claude', self.logs[-1])
        self.assertIn('mid-turn', self.logs[-1])
        self.r.fetch_pr.assert_called_once()

    def test_independently_read_final_notice_allows_exit(self):
        self.prepare_final()
        self.pane.return_value = CLAUDE_RUNNING

        def read_and_advance(seconds):
            self.advance(seconds)
            self.unread['claude'] = []

        with patch.object(relay, 'pause', read_and_advance):
            self.assertEqual(0, self.r.run())
        self.send.assert_not_called()
        self.assertEqual(5, self.t)
        self.assertFalse(self.r.holds)

    def test_final_notice_state_can_resume_a_drain_after_restart(self):
        self.prepare_final()
        self.assertTrue(self.r.watch_pr())
        self.assertEqual([['claude', 'final-claude']], self.r.state['terminal_mail'])
        self.r.hub.write_message.reset_mock()
        self.pane.side_effect = [CLAUDE_RUNNING, CLAUDE_IDLE]
        self.assertEqual(0, self.r.run())
        self.r.hub.write_message.assert_not_called()
        self.assertEqual(['final-claude'], self.r.state['announced'])


if __name__ == "__main__":
    unittest.main()
