#!/usr/bin/env python3
"""closer - the autonomous close after a merge (#27, #33): one implementation, two callers.

The relay runs it after a MERGED PR's final notices are drained. The conductor runs it as a backstop
for a merged member whose relay has gone (#33). Neither closes on a timeout alone.

Stepwise on purpose: `step_helpers()` and `agent_blockers()` each look once and return, keeping
their evidence (settled pane hashes) in the object, so the relay can loop on them while it keeps
delivering mail, and the conductor can advance one check per tick without blocking its queue.

- Helpers (`#N revmux rK`, `#N your review` in this repo's workspace) close on their own evidence,
  first and independently of the agents. wb.py launches them in agwinterm's DIRECT command mode: the
  pane runs the helper with no shell around it, and when it ends the pane stays on screen with its
  input closed - nothing can be typed into it and nothing more is printed. So a helper closes when
  its completion marker exists (`state/helpers/<pane>.done`, written as its last act with the
  pane's rows at that moment), the pane shows exactly those rows, no live shell is in its foreground
  (a helper started with a shell, the old way, never qualifies), and it has been unchanged for
  CLOSE_SETTLE seconds. No prompt parsing.
- The issue session (exactly the two agent panes) closes only when the planner recorded
  `loop-state done` for this PR, no mail is unread, no .git/index.lock exists, and both agent panes
  are idle with a provably empty composer and unchanged for CLOSE_SETTLE seconds.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import agw
import limits

CLOSE_WAIT = 600.0       # how long a close waits for the loop to be provably over (#27)
CLOSE_SETTLE = 30.0      # a pane must be unchanged this long before it may be closed


def issue_from_branch(branch: str) -> str | None:
    match = re.match(r'issue-(\d+)', branch or '')
    return match.group(1) if match else None


def filled_rows(text: str) -> list[str]:
    return [row.rstrip() for row in (text or '').splitlines() if row.strip()][-limits.WINDOW:]


def helper_untouched(marker_rows: list[str], current_rows: list[str]) -> bool:
    """True only when the pane shows exactly what the helper left when it wrote its marker. A direct
    mode helper prints nothing after that and cannot be typed into, so anything else - a prompt, a
    typed command, more output - means the pane is not the finished helper's."""
    return bool(marker_rows) and current_rows == marker_rows


class Closer:
    def __init__(self, hub_dir: Path, repo: str, issue: str | int | None, peers, *, log: Callable[[str], None],
                 clock: Callable[[], float] = time.monotonic, dry_run: bool = False):
        self.hub_dir = Path(hub_dir)
        self.repo = repo
        self.issue = str(issue) if issue else None
        self.workspace_name = repo.split('/')[-1]
        self.peers = peers
        self.echo = log
        self.clock = clock
        self.dry_run = dry_run
        self.settled: dict[str, tuple[str, float]] = {}
        self.decided: dict[str, str] = {}       # helper session id -> last logged decision
        self.deadline = clock() + CLOSE_WAIT

    # --- logging and settings ------------------------------------------------------------------
    def log(self, text: str) -> None:
        """Every step goes to state/relay-close.log BEFORE it is acted on."""
        self.echo(f"close: {text}")
        if self.dry_run:
            return
        path = self.hub_dir / 'state' / 'relay-close.log'
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a', encoding='utf-8') as handle:
            handle.write(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {text}\n")

    def autonomous(self) -> bool:
        try:
            settings = json.loads((self.hub_dir / 'state' / 'implementer.json').read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            return False
        return isinstance(settings, dict) and settings.get('autonomous') is True

    def cleanup_mode(self) -> str:
        """`cleanup` as the launcher recorded it from the config (#41): a missing key is the default,
        `merged`; a value that is not one of the modes is `off` - never delete on a value we cannot read."""
        try:
            settings = json.loads((self.hub_dir / 'state' / 'implementer.json').read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            return 'off'
        value = settings.get('cleanup', 'merged') if isinstance(settings, dict) else None
        if value not in ('merged', 'build', 'off'):
            self.log(f'cleanup {value!r} in state/implementer.json is not merged, build or off: treated as off')
            return 'off'
        return value

    def start_cleanup(self, pr: int) -> None:
        """After the issue session is closed: start the detached after-close (#41), which waits for
        every `#N` session to go and then deletes the checkout if it is safe. Never raises."""
        mode = self.cleanup_mode()
        if mode == 'off' or not self.issue:
            self.log(f'checkout cleanup: {mode}; the checkout stays')
            return
        if self.dry_run:
            self.log(f'[dry-run] would start the checkout cleanup ({mode})')
            return
        import cleanup
        try:
            pid, how = cleanup.start_after_close(self.hub_dir.parent, self.repo, self.issue, pr, mode)
        except (OSError, ValueError) as err:
            self.log(f'could not start the checkout cleanup: {err}')
            return
        self.log(f'checkout cleanup ({mode}) started as pid {pid} ({how})'
                 + (' - it may end with this session: the job refused breakaway and WMI failed' if how == 'detached' and os.name == 'nt' else '')
                 + f'; its log: {self.hub_dir.parent.parent / cleanup.LOG_NAME}')

    def timed_out(self) -> bool:
        return self.clock() >= self.deadline

    def settle(self, pane: str, text: str) -> str | None:
        """None once the pane's tail has been unchanged for CLOSE_SETTLE seconds, else the reason."""
        tail = limits.tail_hash(text)
        seen = self.settled.get(pane)
        if seen is None or seen[0] != tail:
            self.settled[pane] = (tail, self.clock())
            return 'changed'
        if self.clock() - seen[1] < CLOSE_SETTLE:
            return 'settling'
        return None

    # --- helpers ------------------------------------------------------------------------------
    def helper_sessions(self, snapshot):
        if not self.issue:
            return []
        name = re.compile(rf'#{self.issue} (revmux r\d+|your review)')
        return [session for workspace, session in agw.sessions(snapshot)
                if (workspace.get('name') or '').casefold() == self.workspace_name.casefold()
                and name.fullmatch(session.get('name') or '')]

    def marker_path(self, pane: str) -> Path:
        return self.hub_dir / 'state' / 'helpers' / f'{pane}.done'

    def step_helpers(self) -> list[str]:
        """One look at every helper; closes those proven done and untouched. Returns what stays open."""
        left_open = []
        snapshot = agw.tree()
        for session in self.helper_sessions(snapshot):
            panes = agw.panes_of(session)
            reason = self.helper_reason(panes, session)
            label = f"{session.get('name')} ({session.get('id')})"
            if reason:
                left_open.append(f"{session.get('name')} ({reason})")
                if self.decided.get(session.get('id')) != reason:
                    self.decided[session.get('id')] = reason
                    self.log(f"helper {label} stays open: {reason}")
                continue
            if not self.autonomous():
                self.log(f"NOT closing helper {label}: autonomy was turned off")
                left_open.append(f"{session.get('name')} (autonomy off)")
                continue
            if self.dry_run:
                self.log(f"[dry-run] would close helper {label}")
                continue
            self.log(f"closing helper {label}: done and untouched")
            for pane in panes:
                agw.clear_restore(pane)
            agw.close_session(session.get('id'))
            self.marker_path(panes[0]).unlink(missing_ok=True)
        return left_open

    def helper_reason(self, panes: list[str], session: dict) -> str | None:
        if len(panes) != 1:
            return 'not a single-pane helper'
        try:
            marker = json.loads(self.marker_path(panes[0]).read_text(encoding='utf-8-sig'))
        except FileNotFoundError:
            return 'no completion marker (still running, or ended without one)'
        except (OSError, ValueError):
            return 'unreadable completion marker'
        try:
            text = agw.pane_text(panes[0])
        except (agw.CtlError, OSError) as err:
            return f'pane unreadable: {err}'
        shells = session.get('foregroundShells') or [session.get('foregroundShell')]
        if shells and shells[0]:
            # A shell is running in it: started the old way (with a shell), or not a finished helper.
            return f'a {shells[0]} shell is live in it (only a direct-mode helper that has ended closes)'
        if not helper_untouched(marker.get('rows') or [], filled_rows(text)):
            return 'the pane shows something other than what the helper left'
        state = self.settle(panes[0], text)
        return f'pane {state}' if state else None

    # --- the agents and the issue session -----------------------------------------------------
    def agent_blockers(self, number: int) -> list[str]:
        """What still stops the issue-session close. Empty only when the loop is provably over."""
        import hub
        import peerchat
        reasons = []
        try:
            done = json.loads((self.hub_dir / 'state' / 'loop-done.json').read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            done = {}
        if not isinstance(done, dict) or done.get('pr') != number:
            reasons.append(f'the planner has not recorded `wb.py loop-state done --pr {number}`')
        inbox = self.hub_dir / 'inbox'
        for path in sorted((inbox / 'claude').glob('*.md')):
            # Anything the planner has not read - above all a human's "don't close" - stops the close.
            reasons.append(f'the planner has unread mail {path.stem}')
        for path in sorted((inbox / 'codex').glob('*.md')):
            try:
                sender = hub.parse_message(path).get('from')
            except (OSError, ValueError):
                sender = None
            if sender in (None, 'claude', 'human', 'github'):
                reasons.append(f'the implementer has not read {path.stem}')
        if (self.hub_dir.parent / '.git' / 'index.lock').exists():
            reasons.append('.git/index.lock exists')
        for peer in self.peers:
            try:
                text = agw.pane_text(peer.pane)
            except (agw.CtlError, OSError) as err:
                reasons.append(f'{peer.box} pane unreadable: {err}')
                continue
            state = self.settle(peer.pane, text)
            if state:
                reasons.append(f'{peer.box} pane {state}')
            if peerchat.is_busy(text) or (peer.tool == 'codex' and any('Working' in row for row in text.splitlines()[-6:])):
                reasons.append(f'{peer.box} is running a turn')
            profile = peerchat.PROFILES[peer.tool]
            content = (peerchat.claude_composer(text) if peer.tool == 'claude' else peerchat.codex_composer(text))
            if content is None or not peerchat.looks_empty(profile, content):
                reason = f'{peer.box} composer is not provably empty'
                if peer.tool == 'claude':
                    reason += (' (a greyed prompt suggestion? agents the workbench launches have them off;'
                               ' for an adopted Claude set "promptSuggestionEnabled": false in ~/.claude/settings.json)')
                reasons.append(reason)
        return reasons

    def close_issue_session(self) -> None:
        agents = {peer.pane for peer in self.peers}
        snapshot = agw.tree()
        issue_session = next((session for _, session in agw.sessions(snapshot)
                              if set(agw.panes_of(session)) == agents), None)
        if issue_session is None:
            self.log('the issue session is already gone')
            return
        self.log(f"closing the issue session {issue_session.get('name')} ({issue_session.get('id')})")
        for pane in sorted(agents):
            agw.clear_restore(pane)
        agw.close_session(issue_session.get('id'))


def relay_alive(repo: str, issue: str, snapshot) -> bool:
    """Is this issue's relay session (`#N relay` in the repo's workspace) in the tree?"""
    workspace_name = repo.split('/')[-1].casefold()
    return any((workspace.get('name') or '').casefold() == workspace_name and session.get('name') == f'#{issue} relay'
               for workspace, session in agw.sessions(snapshot))
