#!/usr/bin/env python3
"""composer_watch - text that sits typed-but-unsent in an agent's composer is the relay's to fix (#130).

Nobody types into an automatic session's panes by hand. Whatever sits in a composer got there through
the relay: a pointer whose submit key the TUI did not take, a pointer typed over another when the
composer parser missed a draft (Codex's footer hides one, #68; Claude under load draws a typed line
seconds late), or a pointer left behind once its mail was read some other way. Such text makes the
pane look busy to the stall watch and to the close, and every later ring refuses the composer as a
draft, so a member can hang for hours on it.

- `step(texts)` looks at every agent pane with the panes read for it by the caller: the relay's
  `watch_composers` reads them every limit interval while watching and draining, every mail interval
  in its close loop, and the conductor's backstop every conductor tick (`composer_state` reads each
  pane once more, so a look costs two reads per pane). Composer content that is not the tool's empty placeholder is timed
  from the first look that showed it, by its folded text, so a redraw does not restart the clock.
  Content older than `stall_seconds` (config `composerStallSeconds`, default 60; 0 turns the watch
  off) is STALLED: `stalled(box)` says so, and the stall watch no longer counts that composer as a
  turn in progress.
- A stalled composer that holds exactly the relay's pointer for mail still unread is the pointer
  rescue's (#96), which presses the submit key, until the rescue has given up on it
  (`rescue_exhausted`): then the watch clears it so the mail rings afresh.
- Anything else stalled, on an idle pane with no dialog, is cleared with Ctrl+U
  (`peerchat.clear_composer`): copies of a pointer typed over each other, a pointer for mail already
  read, the relay's own probe or resume text, text nobody can account for. Every clear is logged with
  the text it removed and reported to the caller as (peer, mid) for every unread mail whose pointer
  the text carried, so each mail's announcement can be taken back and its ring repeated into the
  empty composer; a clear interrupted after a press (the pane unreadable for a moment) is reported
  on the next look that finds the composer empty; what it carried stays pending only for the ids the
  text no longer shows (a marker still there reports itself: the clear that removes it, or the
  rescue's submit), so a remainder (the marker gone, a prefix kept) carries them into the clear that
  removes it, reported once, however many presses or looks that takes; a pointer that went after all
  (the rescue's submit, a ring: the relay's `rung` calls `sent`) is pending no more. Text that is not the
  relay's alerts the human once. The caller's `skip(peer, message)` names unread mail whose pointer
  is never cleared here, however it is typed (exact, duplicated or mangled): the conductor's backstop
  passes the mail its close waits for, which is logged once and left for the rescue and the human.
- `automatic(peer)` says whose pane it is: a queue member's panes are the workbench's; an adopted
  Claude (the caller's own session, `claude.json` origin `adopted`) is the human's whatever the loop's
  autonomy. In a pane that is not automatic only text the relay typed (it starts with
  `closer.POINTER_LABEL`) is cleared; a human's draft is logged once and left alone.
- While the pane runs a turn nothing is pressed: the clock runs, the key waits for the idle pane, and
  `clear_composer` refuses between presses if a turn starts.

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
                 alert: Callable[[object, str], None], automatic: Callable[[object], bool],
                 rescue_exhausted: Callable[[str, str], bool] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 stall_seconds: float = COMPOSER_STALL_SECONDS):
        self.peers = peers
        self.unread_message = unread_message
        self.pointer = pointer
        self.log = log
        self.alert = alert
        self.automatic = automatic
        self.rescue_exhausted = rescue_exhausted
        self.clock = clock
        self.stall_seconds = stall_seconds
        self.seen: dict[str, tuple[str, float]] = {}      # box -> (folded content, first seen at)
        self.stalled_boxes: set[str] = set()
        self.alerted: set[tuple[str, str]] = set()         # (box, folded content) alerted once
        self.left_alone: set[tuple[str, str]] = set()      # (box, folded content) logged once as not ours
        # box -> the mids a clear interrupted after a press carried (None: it carried no mail)
        self.pending: dict[str, list[str | None]] = {}

    def stalled(self, box: str) -> bool:
        """Text has sat in this box's composer past the stall period."""
        return box in self.stalled_boxes

    def step(self, texts: dict,
             skip: Callable[[object, dict | None], bool] | None = None) -> list[tuple[object, str | None]]:
        """One look at every pane; returns (peer, mid) for each unread mail whose pointer a cleared
        composer carried, (peer, None) for a clear that carried none. Text carrying a pointer for
        unread mail `skip(peer, message)` names is left alone, logged once."""
        import closer
        import limits
        import peerchat
        cleared: list[tuple[object, str | None]] = []
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
                if peer.box in self.pending:
                    # The clear went through after all: report what it carried.
                    for mid in self.pending.pop(peer.box):
                        cleared.append((peer, mid))
                    self.log(f"{peer.box} composer is empty after an interrupted clear")
                continue
            folded = peerchat.compact(content or '')
            pend = self.pending.get(peer.box)
            if pend is not None:
                # What an interrupted clear carried stays pending only for the ids the text no
                # longer shows: a marker still there reports itself (the clear that removes it, or
                # the rescue's submit), whether the clear took nothing or left a remainder that
                # kept it. With nothing left to carry the entry goes.
                shown = peerchat.pointer_ids(content)
                left = [mid for mid in pend if mid is not None and mid not in shown]
                if left:
                    pend = left
                    self.pending[peer.box] = pend
                else:
                    self.pending.pop(peer.box)
                    pend = None
            if state == 'ambiguous':
                # A greyed suggestion or a draft with its caret at the start: not text we can own.
                continue
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
            mids = peerchat.pointer_ids(content)
            unread = [mid for mid in mids if self.unread_message(peer.box, mid) is not None]
            kept = [mid for mid in unread
                    if skip is not None and skip(peer, self.unread_message(peer.box, mid))]
            if kept:
                # Mail the caller never clears a pointer for (the conductor's close waits for it):
                # the text stays, however many copies or whatever else it holds, for the rescue and
                # the human. Logged once.
                if (peer.box, folded) not in self.left_alone:
                    self.left_alone.add((peer.box, folded))
                    self.log(f"stalled text in {peer.box} carries a pointer for {', '.join(kept)}, which "
                             f"the close waits for; left for the rescue and the human: {content[:120]!r}")
                continue
            ours = content.startswith(closer.POINTER_LABEL)
            exact = len(unread) == 1 and len(mids) == 1 and peerchat.owns(
                content, self.pointer(self.unread_message(peer.box, unread[0])))
            if exact:
                # Exactly the pointer for mail still unread: the rescue submits it (#96), until it
                # has given up; then the text goes and the mail rings afresh.
                if self.rescue_exhausted is None or not self.rescue_exhausted(peer.box, unread[0]):
                    continue
                what = f"the pointer for {unread[0]}, which the rescue gave up on"
            elif not ours and not self.automatic(peer):
                # Not the workbench's pane: a human may be drafting here. Only the relay's own text
                # is ever cleared; this is logged once and left.
                if (peer.box, folded) not in self.left_alone:
                    self.left_alone.add((peer.box, folded))
                    self.log(f"stalled text in {peer.box} is not the relay's and the pane is not an "
                             f"automatic session's; left alone: {content[:120]!r}")
                continue
            elif mids:
                what = "pointers for " + ", ".join(mids) if len(mids) > 1 else f"a pointer for {mids[0]}"
                if not unread:
                    what += ", whose mail is not unread"
            elif ours:
                what = "the relay's own text"
            else:
                what = "text that is not a relay pointer"
            carried: list[str | None] = list(unread)
            if pend is not None:
                # A remainder of an interrupted clear: the ids it carried go with this one.
                left = [mid for mid in pend if mid is not None and mid not in carried]
                if left:
                    what += f", the remainder of an interrupted clear that carried {', '.join(left)}"
                    carried.extend(left)
            carried = carried or [None]
            pressed = []
            idle = lambda frame, peer=peer: not closer.agent_busy(peer, frame)  # noqa: E731
            try:
                done = peerchat.clear_composer(peer.pane, profile, content, idle=idle,
                                               on_press=lambda: pressed.append(True))
            except (peerchat.Refused, peerchat.Failed, agw.CtlError, OSError) as err:
                if pressed:
                    # A key went in before the pane stopped answering: the text may be gone. The
                    # next look that finds the composer empty reports what it carried; a remainder
                    # carries it into the clear that removes it.
                    self.pending[peer.box] = carried
                self.log(f"could not clear stalled composer text in {peer.box}: {err}")
                continue
            if not done:
                # The presses took only part of the text: what is left is a remainder, and the ids
                # go with the clear that removes it, as after an interrupted clear.
                self.pending[peer.box] = carried
                self.log(f"could not clear stalled composer text in {peer.box}: not empty after "
                         f"{peerchat.CLEAR_PRESSES} presses")
                continue
            self.log(f"cleared stalled composer text in {peer.box} after {age:.0f}s ({what}): {content[:200]!r}")
            self.forget(peer.box)
            self.pending.pop(peer.box, None)
            cleared.extend((peer, mid) for mid in carried)
            if not ours and (peer.box, folded) not in self.alerted:
                self.alerted.add((peer.box, folded))
                self.alert(peer, f"composer held text for {age:.0f}s that no ring of the relay explains; "
                                 f"cleared: {content[:160]!r}")
        return cleared

    def forget(self, box: str) -> None:
        self.seen.pop(box, None)
        self.stalled_boxes.discard(box)

    def sent(self, box: str, mid: str) -> None:
        """A pointer for this mail went after all (the rescue submitted it, or a ring did): it is
        pending from no interrupted clear, so the empty composer that follows does not report it."""
        left = [m for m in self.pending.get(box, []) if m != mid]
        if left:
            self.pending[box] = left
        else:
            self.pending.pop(box, None)
