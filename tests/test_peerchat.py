"""Submission guards exercised with captured frames; no terminal or network."""

import contextlib
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import agw
import peerchat
import relay
from frames import (CLAUDE_IDLE, CLAUDE_RUNNING, CODEX_IDLE, CODEX_QUEUED, CODEX_UNSUBMITTED,
                    CLAUDE_SUGGESTION, CLAUDE_WRAPPED_DRAFT, TEXT, Clock, FakeAgw, claude, codex, stable_frames)



class Submission(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()

    @contextlib.contextmanager
    def environment(self, fake):
        with patch.object(agw, 'pane_text', fake.pane_text), patch.object(agw, 'type_into', fake.type_into), \
                patch.object(agw, 'cursor_column', fake.cursor_column), \
                patch.object(agw, 'request', side_effect=AssertionError('real terminal request')), \
                patch.object(peerchat, 'now', self.clock.now), patch.object(peerchat, 'pause', self.clock.pause):
            yield

    def send(self, fake, text=TEXT):
        with self.environment(fake):
            try:
                return peerchat.send('pane', peerchat.PROFILES[fake.tool], text, dry_run=False, retry=True)
            finally:
                self.assertEqual(1, fake.keys.count(text), 'a send must never retype its text')

    def test_submitted_first_time_for_both_profiles(self):
        for tool, key in [('codex', '\t'), ('claude', '\n')]:
            with self.subTest(tool=tool):
                fake = FakeAgw(tool)
                self.assertEqual('submitted', self.send(fake))
                self.assertEqual([TEXT, key], fake.keys)

    def test_captured_queue_identifies_this_message_only(self):
        for frame, expected in [(CODEX_QUEUED, 'queued'),
                                (CODEX_QUEUED.replace('193916Z', '193917Z'), 'submitted')]:
            with self.subTest(expected=expected):
                fake = FakeAgw(after=lambda f: frame)
                self.assertEqual(expected, self.send(fake))
                self.assertEqual([TEXT, '\t'], fake.keys)

    def test_queue_label_requires_matching_text_as_well_as_id(self):
        frame = CODEX_QUEUED.replace('step 1:', 'step 2:')
        self.assertEqual('submitted', self.send(FakeAgw(after=lambda f: frame)))

    def test_claude_queued_placeholder_after_our_submit_reports_queued(self):
        frame = claude('Press up to edit queued messages')
        fake = FakeAgw('claude', frames=stable_frames(CLAUDE_IDLE, claude(TEXT), frame))
        self.assertEqual('queued', self.send(fake))
        self.assertEqual([TEXT, '\n'], fake.keys)

    def test_claude_queue_placeholder_before_typing_is_refused_as_a_possible_draft(self):
        frame = claude('Press up to edit queued messages')
        fake = FakeAgw('claude', frames=[frame])
        with self.environment(fake), self.assertRaisesRegex(peerchat.Refused, 'holds a draft'):
            peerchat.send_once('pane', peerchat.PROFILES['claude'], TEXT, dry_run=False)
        self.assertEqual([], fake.keys)

    def test_claude_queue_placeholder_with_extra_content_is_not_empty(self):
        for extra in [' and an unsent draft', '\n  1. Yes\n  2. No']:
            frame = claude('Press up to edit queued messages' + extra)
            self.assertFalse(peerchat.looks_empty(peerchat.PROFILES['claude'],
                                                  peerchat.claude_composer(frame)))
            fake = FakeAgw('claude', after=lambda f: frame)
            with self.assertRaises(peerchat.Failed):
                self.send(fake)
            self.assertEqual([TEXT, '\n'], fake.keys)

    def test_clipped_queue_prefix_needs_an_id_and_later_entries_are_checked(self):
        clipped = TEXT.split('[id ', 1)[0].rstrip()
        self.assertFalse(peerchat.owns(clipped, TEXT))
        queue = '• Queued follow-up inputs\n  ↳ ' + clipped + '\n'
        for entries, outcome in [(queue, 'submitted'), (queue + '  ↳ ' + TEXT + '\n', 'queued')]:
            with self.subTest(outcome=outcome):
                frame = codex('Ask Codex to do anything', entries)
                self.assertEqual(outcome, self.send(FakeAgw(after=lambda f: frame)))

    def test_submit_and_queue_after_retries_report_the_path(self):
        for retry, completed, expected in [(1, CODEX_IDLE, 'submitted after retry 1'),
                                            (2, CODEX_QUEUED, 'queued after retry 2')]:
            with self.subTest(retry=retry):
                fake = FakeAgw(after=lambda f: completed if len(f.keys) >= retry + 2 else codex(TEXT))
                self.assertEqual(expected, self.send(fake))
                self.assertEqual([TEXT] + ['\t'] * (retry + 1), fake.keys)

    def test_idle_codex_uses_one_return_after_tabs_fail(self):
        fake = FakeAgw(after=lambda f: CODEX_IDLE if f.keys[-1] == '\n' else codex(TEXT))
        self.assertEqual('submitted after Return', self.send(fake))
        self.assertEqual([TEXT, '\t', '\t', '\t', '\n'], fake.keys)

    def test_exhausted_retries_quote_the_composer_without_retyping(self):
        for tool, keys in [('codex', ['\t', '\t', '\t', '\n']), ('claude', ['\n'] * 3)]:
            with self.subTest(tool=tool):
                fake = FakeAgw(tool, after=lambda f: codex(TEXT) if f.tool == 'codex' else claude(TEXT))
                with self.assertRaisesRegex(peerchat.Failed, 'pointer still unsent') as caught:
                    self.send(fake)
                self.assertIn(repr(TEXT), str(caught.exception))
                self.assertEqual([TEXT] + keys, fake.keys)

    def test_busy_or_queued_evidence_anywhere_vetoes_return(self):
        for marker, padding in [('Working (esc to interrupt)', 0), ('Working (esc to interrupt)', 20),
                                ('Working (esc to interrupt)', 50), ('Queued follow-up inputs', 50)]:
            with self.subTest(marker=marker, padding=padding):
                frame = codex(TEXT, marker + '\n' + 'old row\n' * padding)
                fake = FakeAgw(after=lambda f: frame)
                with self.assertRaises(peerchat.Failed):
                    self.send(fake)
                self.assertEqual([TEXT, '\t', '\t', '\t'], fake.keys)

    def test_old_same_id_queue_cannot_hide_unsent_or_changed_composer(self):
        for draft in [TEXT, 'check if codex replied']:
            with self.subTest(draft=draft):
                frame = CODEX_QUEUED.replace('› Ask Codex to do anything', '› ' + draft)
                fake = FakeAgw(after=lambda f: frame)
                with self.assertRaises(peerchat.Failed):
                    self.send(fake)
                expected = ['\t'] * (3 if draft == TEXT else 1)
                self.assertEqual([TEXT] + expected, fake.keys)

    def test_changed_drafts_fail_before_first_submit_and_during_polling(self):
        for draft in ['check if codex replied', TEXT + ' and more', TEXT.replace('step 1:', 'step 2:')]:
            for before_submit in [True, False]:
                with self.subTest(draft=draft, before_submit=before_submit):
                    fake = (FakeAgw(frames=stable_frames(CODEX_IDLE, codex(draft))) if before_submit
                            else FakeAgw(after=lambda f: codex(draft)))
                    with self.assertRaisesRegex(peerchat.Failed, 'other than the attempted pointer'):
                        self.send(fake)
                    self.assertEqual([TEXT] + ([] if before_submit else ['\t']), fake.keys)

    def test_short_wrapped_and_clipped_text_can_be_verified(self):
        marker_start, marker_end = TEXT.index('[id '), TEXT.index(']') + 1
        cases = [('hello', 'hello'), (TEXT, TEXT[:70] + '\n  ' + TEXT[70:]),
                 (TEXT, TEXT[:marker_end]), (TEXT, TEXT[marker_start:]),
                 (TEXT, TEXT[marker_start:marker_end + 10])]
        for text, rendered in cases:
            with self.subTest(rendered=rendered):
                fake = FakeAgw(frames=stable_frames(CODEX_IDLE, codex(rendered), CODEX_IDLE))
                self.assertEqual('submitted', self.send(fake, text))
                self.assertEqual([text, '\t'], fake.keys)

    def test_clipped_pointer_without_complete_id_never_authorizes_a_key(self):
        for visible in [TEXT[:30], TEXT[:TEXT.index('[id ')], TEXT[:TEXT.index(']')]]:
            with self.subTest(visible=visible):
                self.assertFalse(peerchat.owns(visible, TEXT))
                fake = FakeAgw(frames=stable_frames(CODEX_IDLE, codex(visible)))
                with self.assertRaises(peerchat.Failed):
                    self.send(fake)
                self.assertEqual([TEXT], fake.keys)

    def test_unlabelled_clipped_text_needs_at_least_half_the_message(self):
        text = 'An ordinary message without an id marker and with sufficient text to clip.'
        self.assertTrue(peerchat.owns(text, text))
        self.assertTrue(peerchat.owns(text[:len(text) // 2 + 1], text))
        self.assertFalse(peerchat.owns(text[:24], text))

    def test_captured_unsubmitted_frame_really_holds_a_pointer(self):
        content = peerchat.codex_composer(CODEX_UNSUBMITTED)
        self.assertIn('[id 20260922T221152Z-claude-7be1]', content)
        fake = FakeAgw(frames=stable_frames(CODEX_IDLE, CODEX_UNSUBMITTED))
        with self.assertRaisesRegex(peerchat.Failed, 'pointer still unsent'):
            self.send(fake, content)
        self.assertEqual([content, '\t', '\t', '\t', '\n'], fake.keys)

    def test_dialog_and_disappearance_after_submit_withhold_further_keys(self):
        for frame, reason in [(codex(TEXT) + '\n› 1. Yes', 'dialog'), ('─' * 50, 'disappeared')]:
            with self.subTest(reason=reason):
                fake = FakeAgw(after=lambda f: frame)
                with self.assertRaisesRegex(peerchat.Failed, reason):
                    self.send(fake)
                self.assertEqual([TEXT, '\t'], fake.keys)

    def test_fresh_retry_boundary_rejects_dialog_disappearance_or_new_draft(self):
        for changed in [codex(TEXT) + '\n› 1. Yes', '─' * 50, codex('another draft')]:
            with self.subTest(changed=changed):
                deadline_seen = False

                def after(fake):
                    nonlocal deadline_seen
                    if deadline_seen:
                        return changed
                    if self.clock.t >= 2 * peerchat.SETTLE + peerchat.SUBMIT_TIMEOUT:
                        deadline_seen = True
                    return codex(TEXT)

                self.clock.t = 0
                fake = FakeAgw(after=after)
                with self.assertRaises(peerchat.Failed):
                    self.send(fake)
                self.assertTrue(deadline_seen)
                self.assertEqual([TEXT, '\t'], fake.keys)

    def test_post_write_control_errors_are_failed_with_the_phase(self):
        cases = [('read', 3, 'verifying typed text'), ('read', 4, 'verifying typed text'),
                 ('read', 5, 'verifying submit'), ('read', 6, 'verifying submit'),
                 ('type', 1, 'typing text'), ('type', 2, 'submitting'), ('type', 3, 'retry 1'),
                 ('type', 5, 'Return fallback')]
        for kind, index, phase in cases:
            for error_type in [agw.CtlError, OSError]:
                with self.subTest(kind=kind, index=index, error_type=error_type):
                    fake = FakeAgw(after=lambda f: codex(TEXT))
                    errors = fake.read_errors if kind == 'read' else fake.type_errors
                    errors[index] = error_type('lost terminal')
                    with self.assertRaisesRegex(peerchat.Failed, phase):
                        self.send(fake)

    def test_precheck_control_error_stays_prewrite(self):
        fake = FakeAgw()
        fake.read_errors[1] = agw.CtlError('not reachable')
        with self.environment(fake), self.assertRaises(agw.CtlError):
            peerchat.send_once('pane', peerchat.PROFILES['codex'], TEXT, dry_run=False)
        self.assertEqual([], fake.keys)

    def test_dry_run_only_prechecks(self):
        fake = FakeAgw()
        with self.environment(fake), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual('dry-run', peerchat.send_once('pane', peerchat.PROFILES['codex'], TEXT, dry_run=True))
        self.assertEqual([], fake.keys)
        self.assertEqual(2, fake.reads)

    def test_claude_classification_preserves_ambiguous_and_wrapped_drafts(self):
        for frame, cursor, expected in [
                (CLAUDE_IDLE, 2, 'empty'), (claude('Try "a suggestion"'), 2, 'empty'),
                (CLAUDE_SUGGESTION, 2, 'ambiguous'),
                (claude('a real draft with Home pressed'), 2, 'ambiguous'),
                (claude('a real draft'), 14, 'draft'), (CLAUDE_WRAPPED_DRAFT, 2, 'draft'),
                (claude('another draft\n'), 2, 'draft'),
                (CLAUDE_SUGGESTION, agw.CtlError('unsupported'), 'draft'),
                (CLAUDE_SUGGESTION, OSError('unavailable'), 'draft'),
                (CLAUDE_SUGGESTION.replace('\n> ', '\n  > '), 4, 'ambiguous')]:
            with self.subTest(cursor=cursor, expected=expected):
                fake = FakeAgw('claude', frames=[frame], cursors=[cursor])
                with self.environment(fake):
                    text = agw.pane_text('pane')
                    content, state, _ = peerchat.composer_state('pane', peerchat.PROFILES['claude'], text)
                self.assertEqual(expected, state)
                self.assertEqual(peerchat.claude_composer(frame), content)
                if fake.cursor_reads:
                    self.assertEqual(['text', 'cursor', 'text'], fake.events)

    def test_ambiguous_and_draft_prechecks_never_type(self):
        for cursor, error, reason in [(2, peerchat.AmbiguousComposer, 'possibly a suggestion'),
                                      (30, peerchat.Refused, 'holds a draft')]:
            fake = FakeAgw('claude', frames=[CLAUDE_SUGGESTION], cursors=[cursor])
            with self.environment(fake), self.assertRaisesRegex(error, reason) as caught:
                peerchat.send_once('pane', peerchat.PROFILES['claude'], TEXT, dry_run=False)
            self.assertEqual([], fake.keys)
            if error is peerchat.AmbiguousComposer:
                self.assertEqual(peerchat.claude_composer(CLAUDE_SUGGESTION), caught.exception.content)

    def test_dialog_still_refuses_even_with_cursor_at_start(self):
        fake = FakeAgw('claude', frames=[CLAUDE_SUGGESTION + '\n> 1. Yes'], cursors=[2])
        with self.environment(fake), self.assertRaisesRegex(peerchat.Refused, 'dialog'):
            peerchat.precheck('pane', peerchat.PROFILES['claude'])
        self.assertEqual([], fake.keys)
        self.assertEqual(0, fake.cursor_reads)

    def test_malformed_cursor_reply_keeps_text_only_refusal(self):
        cursor_reader = agw.cursor_column
        fake = FakeAgw('claude', frames=[CLAUDE_SUGGESTION])
        with self.environment(fake), patch.object(agw, 'cursor_column', cursor_reader), \
                patch.object(agw, 'request', return_value={'column': 2}), \
                self.assertRaisesRegex(peerchat.Refused, 'holds a draft'):
            peerchat.precheck('pane', peerchat.PROFILES['claude'])
        self.assertEqual([], fake.keys)

    def test_suggestion_only_after_typing_waits_then_withholds_submit(self):
        fake = FakeAgw('claude', frames=stable_frames(CLAUDE_IDLE, CLAUDE_SUGGESTION), cursors=[2])
        with self.assertRaisesRegex(peerchat.Failed, 'typed text was not confirmed'):
            self.send(fake)
        self.assertEqual([TEXT], fake.keys)
        self.assertGreater(fake.cursor_reads, 1)
        self.assertAlmostEqual(peerchat.SETTLE + peerchat.VERIFY_TIMEOUT, self.clock.t)

    def test_post_submit_ambiguity_waits_but_does_not_prove_success(self):
        for other in [CLAUDE_SUGGESTION, claude(TEXT[:30])]:
            with self.subTest(other=other):
                self.clock.t = 0
                fake = FakeAgw('claude', frames=stable_frames(CLAUDE_IDLE, claude(TEXT), other), cursors=[2])
                with self.assertRaisesRegex(peerchat.Failed, 'submit not proven'):
                    self.send(fake)
                self.assertEqual([TEXT, '\n'], fake.keys)
                self.assertGreater(fake.cursor_reads, 2)
                self.assertAlmostEqual(2 * peerchat.SETTLE + peerchat.SUBMIT_TIMEOUT, self.clock.t)

    def test_ambiguity_can_resolve_to_empty_within_submit_window(self):
        fake = FakeAgw('claude', frames=stable_frames(CLAUDE_IDLE, claude(TEXT), CLAUDE_SUGGESTION, CLAUDE_IDLE), cursors=[2])
        self.assertEqual('submitted', self.send(fake))
        self.assertEqual([TEXT, '\n'], fake.keys)

    def test_own_pointer_at_start_still_uses_only_existing_key_retries(self):
        fake = FakeAgw('claude', after=lambda f: claude(TEXT), cursors=[2])
        with self.assertRaisesRegex(peerchat.Failed, 'pointer still unsent'):
            self.send(fake)
        self.assertEqual([TEXT, '\n', '\n', '\n'], fake.keys)

    def test_codex_does_not_read_cursor_or_gain_ambiguity(self):
        fake = FakeAgw('codex', frames=[codex('a real draft')], cursors=[2])
        with self.environment(fake), self.assertRaisesRegex(peerchat.Refused, 'not empty'):
            peerchat.precheck('pane', peerchat.PROFILES['codex'])
        self.assertEqual(0, fake.cursor_reads)
        self.assertEqual([], fake.keys)

    def test_precheck_requires_two_equal_empty_snapshots(self):
        for tool, idle, render in [('claude', CLAUDE_IDLE, claude), ('codex', CODEX_IDLE, codex)]:
            with self.subTest(tool=tool):
                fake = FakeAgw(tool, frames=[idle, render('new draft')], cursors=[2])
                with self.environment(fake), self.assertRaisesRegex(peerchat.Refused, 'changed'):
                    peerchat.send_once('pane', peerchat.PROFILES[tool], TEXT, dry_run=False)
                self.assertEqual([], fake.keys)
                self.assertEqual(2, fake.reads)

    def test_redraw_during_cursor_read_withholds_first_submit(self):
        fake = FakeAgw('claude', frames=stable_frames(CLAUDE_IDLE) + [claude(TEXT), claude('new draft')], cursors=[2])
        with self.assertRaises(peerchat.Failed):
            self.send(fake)
        self.assertEqual([TEXT], fake.keys)
        self.assertEqual(['text', 'cursor', 'text'], fake.events[3:6])

    def test_changed_geometry_waits_for_a_stable_poll_before_first_submit(self):
        wrapped = claude(TEXT.replace(' ', '\n', 1))
        self.assertEqual(peerchat.claude_composer(claude(TEXT)), peerchat.claude_composer(wrapped))
        fake = FakeAgw('claude', frames=stable_frames(CLAUDE_IDLE) + [claude(TEXT), wrapped] +
                      stable_frames(wrapped, CLAUDE_IDLE), cursors=[2])
        self.assertEqual('submitted', self.send(fake))
        self.assertEqual([TEXT, '\n'], fake.keys)
        self.assertAlmostEqual(2 * peerchat.SETTLE + .25, self.clock.t)

    def test_second_snapshot_dialog_withholds_typing_and_submit(self):
        for after_typing in [False, True]:
            with self.subTest(after_typing=after_typing):
                frames = stable_frames(CLAUDE_IDLE) if after_typing else []
                frame = claude(TEXT) if after_typing else CLAUDE_IDLE
                fake = FakeAgw('claude', frames=frames + [frame, frame + '\n> 1. Yes'], cursors=[2])
                with self.environment(fake), self.assertRaisesRegex(peerchat.Failed if after_typing else peerchat.Refused, 'dialog'):
                    peerchat.send_once('pane', peerchat.PROFILES['claude'], TEXT, dry_run=False)
                self.assertEqual([TEXT] if after_typing else [], fake.keys)

    def test_redraw_at_retry_boundary_withholds_retry_key(self):
        for tool, render, key in [('claude', claude, '\n'), ('codex', codex, '\t')]:
            with self.subTest(tool=tool):
                self.clock.t = 0
                boundary_reads = 0
                def after(fake):
                    nonlocal boundary_reads
                    if self.clock.t >= 2 * peerchat.SETTLE + peerchat.SUBMIT_TIMEOUT:
                        boundary_reads += 1
                        if boundary_reads >= 4:
                            return render('new draft')
                    return render(TEXT)
                fake = FakeAgw(tool, after=after, cursors=[2])
                with self.assertRaises(peerchat.Failed):
                    self.send(fake)
                self.assertEqual([TEXT, key], fake.keys)
                self.assertEqual(4, boundary_reads)

    def test_redraw_at_return_fallback_withholds_return(self):
        for changed in [codex('new draft'), codex(TEXT, 'Working (esc to interrupt)')]:
            with self.subTest(changed=changed):
                self.clock.t = 0
                started = None
                boundary_reads = 0
                def after(fake):
                    nonlocal started, boundary_reads
                    if len(fake.keys) == 4:
                        if started is None:
                            started = self.clock.t
                        if self.clock.t >= started + peerchat.SUBMIT_TIMEOUT:
                            boundary_reads += 1
                            if boundary_reads >= 4:
                                return changed
                    return codex(TEXT)
                fake = FakeAgw(after=after)
                with self.assertRaises(peerchat.Failed):
                    self.send(fake)
                self.assertEqual([TEXT, '\t', '\t', '\t'], fake.keys)
                self.assertEqual(4, boundary_reads)

    def test_cli_prints_valid_json(self):
        fake = FakeAgw()
        output = io.StringIO()
        with self.environment(fake), patch.object(sys, 'argv', ['peerchat', '--to', 'codex', '--text', 'hello', '--label', '']), \
                patch.object(peerchat, 'resolve_target', return_value=('pane', peerchat.PROFILES['codex'], 'codex')), \
                patch.object(agw, 'my_pane', return_value='other'), contextlib.redirect_stdout(output):
            self.assertEqual(0, peerchat.main())
        self.assertEqual({'sent': 'submitted', 'to': 'codex', 'pane': 'pane'}, json.loads(output.getvalue()))

    def test_dry_run_cli_stdout_is_json_and_diagnostics_are_stderr(self):
        fake = FakeAgw()
        output, diagnostics = io.StringIO(), io.StringIO()
        with self.environment(fake), patch.object(sys, 'argv', ['peerchat', '--to', 'codex', '--text', 'hello', '--label', '', '--dry-run']), \
                patch.object(peerchat, 'resolve_target', return_value=('pane', peerchat.PROFILES['codex'], 'codex')), \
                patch.object(agw, 'my_pane', return_value='other'), contextlib.redirect_stdout(output), \
                contextlib.redirect_stderr(diagnostics):
            self.assertEqual(0, peerchat.main())
        self.assertEqual({'sent': 'dry-run', 'to': 'codex', 'pane': 'pane'}, json.loads(output.getvalue()))
        self.assertIn('[dry-run] would type', diagnostics.getvalue())
        self.assertEqual([], fake.keys)
        self.assertEqual(2, fake.reads)


class CursorTransport(unittest.TestCase):
    def test_non_object_envelopes_on_pipe_are_refused_without_cli_fallback(self):
        for raw in [b'[]', b'null', b'"text"']:
            with self.subTest(raw=raw), patch.object(agw, '_open_pipe', return_value=io.BytesIO()), \
                    patch.object(agw, '_pipe_roundtrip', return_value=raw), \
                    patch.object(agw, '_via_cli', side_effect=AssertionError('invalid pipe reply must not fall back')), \
                    self.assertRaisesRegex(agw.CtlError, 'non-object envelope'):
                agw.request('surface.cursor', target='pane')

    def test_non_object_envelopes_on_cli_are_control_errors(self):
        for raw in [b'[]', b'null', b'"text"']:
            with self.subTest(raw=raw), patch.object(agw, '_open_pipe', return_value=None), \
                    patch.object(agw, 'ctl_path', return_value='stub-ctl'), \
                    patch.object(agw, '_console_utf8', return_value=lambda: None), \
                    patch.object(agw.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, raw, b'')), \
                    self.assertRaisesRegex(agw.CtlError, 'non-object envelope'):
                agw.request('surface.cursor', target='pane')

    def test_decimal_conversion_limit_is_reported_as_control_error(self):
        previous = sys.get_int_max_str_digits()
        limit = sys.int_info.str_digits_check_threshold
        self.addCleanup(sys.set_int_max_str_digits, previous)
        sys.set_int_max_str_digits(limit)
        value = '1' * (limit + 1)
        self.assertTrue(value.isascii() and value.isdecimal())
        with self.assertRaises(ValueError):
            int(value)
        with patch.object(agw, 'request', return_value=value), self.assertRaises(agw.CtlError):
            agw.cursor_column('pane')

    def test_pipe_accepts_integer_or_decimal_string(self):
        for value in [0, 2, '2', ' 2\n', 80]:
            with self.subTest(value=value), patch.object(agw, '_via_pipe', return_value={'ok': True, 'result': value}) as pipe, \
                    patch.object(agw, '_via_cli', side_effect=AssertionError('unexpected CLI')):
                self.assertEqual(int(value), agw.cursor_column('pane'))
                self.assertEqual({'cmd': 'surface.cursor', 'target': 'pane'}, pipe.call_args.args[0])

    def test_refused_and_malformed_replies_are_control_errors(self):
        replies = [{'ok': False, 'error': 'unsupported'}]
        replies += [{'ok': True, 'result': value} for value in
                    [None, True, False, -1, 2.0, '2.0', '-1', '', 'column 2', {}, [2]]]
        for reply in replies:
            with self.subTest(reply=reply), patch.object(agw, '_via_pipe', return_value=reply), \
                    self.assertRaises(agw.CtlError):
                agw.cursor_column('pane')

    def test_cli_fallback_keeps_cursor_verb_target_and_json_envelope(self):
        response = subprocess.CompletedProcess([], 0, b'{"ok":true,"result":2}', b'')
        with patch.object(agw, '_via_pipe', return_value=None), \
                patch.object(agw, 'ctl_path', return_value='stub-ctl'), \
                patch.object(agw, '_console_utf8', return_value=lambda: None), \
                patch.object(agw.subprocess, 'run', return_value=response) as run:
            self.assertEqual(2, agw.cursor_column('pane'))
        self.assertEqual(['stub-ctl', 'surface', 'cursor', '--target', 'pane', '--json'], run.call_args.args[0])


class CapturedBusyFrames(unittest.TestCase):
    def test_activity_matches_captured_frames(self):
        for frame, expected in [(CLAUDE_IDLE, False), (claude('check if codex replied'), False),
                                (CLAUDE_RUNNING, True), (CODEX_IDLE, False), (CODEX_QUEUED, True),
                                (CODEX_UNSUBMITTED, False)]:
            with self.subTest(frame=frame):
                self.assertEqual(expected, peerchat.is_busy(frame))

    def test_claude_spinner_remains_busy_above_a_tall_tip_block(self):
        frame = CLAUDE_RUNNING.replace('\n  ⎿', '\n' + '  more tip details\n' * 20 + '  ⎿')
        self.assertNotIn('Hullaballooing', '\n'.join(frame.splitlines()[-15:]))
        self.assertTrue(relay.is_busy(frame))

    def test_claude_hours_and_upload_phase_are_busy(self):
        for activity in ['1h 2m 3s · ↓ 4.9k tokens', '45s · ↑ 1.2k tokens', '2m 30s · thinking']:
            with self.subTest(activity=activity):
                frame = CLAUDE_RUNNING.replace('2m 30s · ↓ 4.9k tokens', activity)
                self.assertTrue(relay.is_busy(frame))
        self.assertFalse(relay.is_busy(CLAUDE_IDLE))
