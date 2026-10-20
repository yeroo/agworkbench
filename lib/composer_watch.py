#!/usr/bin/env python3
"""composer_watch - text that sits typed-but-unsent in an agent's composer is the relay's to fix (#130).

Nobody types into an automatic session's panes by hand. Whatever sits in a composer got there through
the relay: a pointer whose submit key the TUI did not take, a pointer typed over another when the
composer parser missed a draft (Codex's footer hides one, #68), or a pointer left behind once its mail
was read some other way. Such text makes the pane look busy to the stall watch and to the close, and
every later ring refuses the composer as a draft, so a member can hang for hours on it.

- `step(texts)` looks at every agent pane once per limit interval, with the texts the limit check
  read. Composer content that is not the tool's empty placeholder is timed from the first look that
  showed it, by its folded text, so a redraw does not restart the clock. Content older than
  `stall_seconds` (config `composerStallSeconds`, default 60; 0 turns the watch off) is STALLED:
  `stalled(box)` says so, and the stall watch no longer counts that composer as a turn in progress.
- A stalled composer that holds exactly the relay's pointer for mail still unread is the pointer
  rescue's (#96), which presses the submit key; this watch leaves it to that.
- Anything else stalled, on an idle pane with no dialog, is cleared with Ctrl+U
  (`peerchat.clear_composer`): copies of a pointer typed over each other, a pointer for mail already
  read, text nobody can account for. The relay rings the unread mail again on its next tick, into an
  empty composer. Every clear is logged with the text it removed; text that is not a relay pointer
  alerts the human once.
- While the pane runs a turn nothing is pressed: the clock runs, the key waits for the idle pane.

The counts live in memory; a restart starts them over, so a restart only makes a clear later.
"""
from __future__ import annotations

import time
from typing import Callable

import agw

COMPOSER_STALL_SECONDS = 60.0


class ComposerWatch:
    def __init__(self, peers, *, unread_message: Callable[[str, str], dict | None],
                 pointer: Callable[[dict], str], log: Callable[[str], None],
                 alert: Callable[[object, str], None], clock: Callable[[], float] = time.monotonic,
                 stall_seconds: float = COMPOSER_STALL_SECONDS):
        self.peers = peers
        self.unread_message = unread_message
        self.pointer = pointer
        self.log = log
        self.alert = alert
        self.clock = clock
        self.stall_seconds = stall_seconds
        self.seen: dict[str, tuple[str, float]] = {}      # box -> (folded content, first seen at)
        self.stalled_boxes: set[str] = set()
        self.alerted: set[tuple[str, str]] = set()         # (box, folded content) alerted once

    # --- what the rest of the relay asks ------------------------------------------------------
    def stalled(self, box: str) -> bool:
        """Text has sat in this box's composer past the stall period."""
        return box in self.stalled_boxes

    # --- the look -------------------------------------------------------------------------------
    def step(self, texts: dict) -> list[str]:
        """One look at every pane; returns the boxes whose composer it cleared."""
        import closer
        import limits
        import peerchat
        cleared: list[str] = []
        if self.stall_seconds <= 0:
            return cleared
        for peer in self.peers:
            text = texts.get(peer.box)
            if not isinstance(text, str) or peer.tool not in peerchat.PROFILES:
                continue
            if limits.ps_prompt_last(text):
                # No agent in the pane (#98): nothing holds a draft.
                self.forget(peer.box)
                continue
            profile = peerchat.PROFILES[peer.tool]
            try:
                content, state, fresh = peerchat.composer_state(peer.pane, profile, text)
            except (agw.CtlError, OSError) as err:
                self.log(f"composer watch: could not read {peer.box}: {err}")
                continue
            if state in ('missing', 'changing', 'dialog'):
                # Unreadable or moving: the clock keeps what it has, nothing is judged on this look.
                continue
            if state == 'empty':
                self.forget(peer.box)
                continue
            if state == 'ambiguous':
                # A greyed suggestion or a draft with its caret at the start: not text we can own.
                continue
            folded = peerchat.compact(content or '')
            since = self.seen.get(peer.box)
            if since is None or since[0] != folded:
                self.seen[peer.box] = (folded, self.clock())
                self.stalled_boxes.discard(peer.box)
                continue
            age = self.clock() - since[1]
            if age < self.stall_seconds:
                continue
            if peer.box not in self.stalled_boxes:
                self.stalled_boxes.add(peer.box)
                self.log(f"composer text stalled in {peer.box} after {age:.0f}s: {content[:120]!r}")
            if closer.agent_busy(peer, fresh) or peerchat.dialog_visible(fresh):
                # The clock runs; the key waits for the idle pane.
                continue
            mid = peerchat.pointer_id(content)
            message = self.unread_message(peer.box, mid) if mid else None
            if message is not None and peerchat.owns(content, self.pointer(message)):
                # Exactly the pointer for mail still unread: the rescue submits it (#96).
                continue
            what = f"a pointer for {mid}" if mid else "text that is not a relay pointer"
            try:
                done = peerchat.clear_composer(peer.pane, profile, content)
            except (peerchat.Refused, peerchat.Failed, agw.CtlError, OSError) as err:
                self.log(f"could not clear stalled composer text in {peer.box}: {err}")
                continue
            if not done:
                self.log(f"could not clear stalled composer text in {peer.box}: not empty after "
                         f"{peerchat.CLEAR_PRESSES} presses")
                continue
            self.log(f"cleared stalled composer text in {peer.box} after {age:.0f}s ({what}): {content[:200]!r}")
            self.forget(peer.box)
            cleared.append(peer.box)
            if not mid and (peer.box, folded) not in self.alerted:
                self.alerted.add((peer.box, folded))
                self.alert(peer, f"composer held text for {age:.0f}s that no ring of the relay explains; "
                                 f"cleared: {content[:160]!r}")
        return cleared

    def forget(self, box: str) -> None:
        self.seen.pop(box, None)
        self.stalled_boxes.discard(box)
