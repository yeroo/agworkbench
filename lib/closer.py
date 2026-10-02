#!/usr/bin/env python3
"""closer - the autonomous close after a merge or closed no-op issue: one implementation, two callers.

The relay runs it after a MERGED PR's final notices are drained or a closed no-op issue is recorded.
The conductor runs it as a backstop for a terminal member whose relay has gone (#33). Neither closes
on a timeout alone: after CLOSE_WAIT only unread implementer mail from before the merge or no-PR
done record is overridden (#44); every other blocker still refuses. No-PR closes require a fresh
GitHub CLOSED check before touching a helper or issue session.

Stepwise on purpose: `step_helpers()` and `agent_blockers()` each look once and return, keeping
their evidence (settled pane hashes) in the object, so the relay can loop on them while it keeps
delivering mail, and the conductor can advance one check per tick without blocking its queue.

- Helpers (`#N revmux rK`, `#N your review`, `#N suite <label>` in the checkout's workspace, `workspace_of`) close on their own evidence,
  first and independently of the agents. wb.py launches them in agwinterm's DIRECT command mode: the
  pane runs the helper with no shell around it, and when it ends the pane stays on screen with its
  input closed - nothing can be typed into it and nothing more is printed. So a helper closes when
  its completion marker exists (`state/helpers/<pane>.done`, written as its last act with the
  pane's rows at that moment), the pane shows exactly those rows, no live shell is in its foreground
  (a helper started with a shell, the old way, never qualifies), and it has been unchanged for
  CLOSE_SETTLE seconds. No prompt parsing.
- The issue session (exactly the two agent panes) closes only when the planner recorded
  `loop-state done` for this PR or a `done --no-pr` record for this issue, no mail is unread,
  no .git/index.lock exists, and both agent panes
  are idle with a provably empty composer and unchanged for CLOSE_SETTLE seconds. Before each look a
  relay pointer left in a composer is dealt with (#96): one for mail still unread is submitted
  (`PointerRescue`), one whose mail needs no reading is cleared with Ctrl+U (`clear_stale_pointers`).
- Finished helpers close early, on every loop (#84): the relay runs `step_finished_helpers` on its own
  timer, outside the autonomous close and whatever `autonomous` says. Its trigger is the result mail:
  a revmux or suite helper closes on the first look after the mail its completion marker names
  (`mail`, in box `to`) has been read. The marker must exist, since it names the mail, but the pane
  is not read: the owner's rule is that once the result is read the session goes, whatever it
  shows - its report or log is already saved. The human's revdiff (`your review`) never closes
  this way. While the autonomous close runs, the relay is inside it and this step does not run:
  `step_helpers` covers that window.
- Mail the implementer never has to act on does not count as unread (#44): the relay's own final
  notices for this PR (`github-pr<N>-...`), and anything created at or after the merge or no-PR done
  time (`close_merged_at` in relay.json). Other unread implementer mail is a soft blocker: it waits for
  CLOSE_WAIT, and then the close goes ahead and logs the ids.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import agw
import limits

CLOSE_WAIT = 600.0       # how long a close waits for the loop to be provably over (#27)
CLOSE_SETTLE = 30.0      # a pane must be unchanged this long before it may be closed
NO_PR = 'no-pr'
# The helper sessions wb.py opens, after the `#N `: a revmux round, the human's revdiff, a suite (#45).
HELPER_NAMES = r'revmux r\d+|your review|suite [A-Za-z0-9._-]+'
MAIL_ID_RE = re.compile(r'[A-Za-z0-9._-]+')
AGMSG = Path(__file__).resolve().parent / 'agmsg.py'   # the path every relay pointer names
POINTER_LABEL = "Chat from Workbench: "   # what every relay pointer starts with
RESCUE_ATTEMPTS = 3      # rescue submits of one pointer before it is reported UNSUBMITTED (#96)
ALERT_EVERY = 300.0      # between two alerts about the same unsubmitted pointer


def parse_time(value) -> datetime | None:
    """A zoned ISO time (GitHub's `mergedAt`, the hub's `created:`) in UTC, else None. Also the relay's
    `timestamp` (relay imports closer, not the other way round)."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def issue_from_branch(branch: str) -> str | None:
    match = re.match(r'issue-(\d+)', branch or '')
    return match.group(1) if match else None


def pending_key(number: int | None) -> int | str:
    return NO_PR if number is None else number


def pending_number(key: int | str) -> int | None:
    return None if key == NO_PR else key


def workspace_of(hub_dir: Path, repo: str) -> str:
    """The agwinterm workspace of this checkout's sessions (#66): the one its queue membership records
    (a named queue's own), else the repo's name - no membership (a manual launch), or one written
    before #66. An unreadable membership raises ValueError: a wrong guess would find no sessions, and
    "nothing is live" is what lets a close or a cleanup go ahead."""
    path = Path(hub_dir) / 'state' / 'queue-member.json'
    if not path.exists():
        return repo.split('/')[-1]
    try:
        membership = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as err:
        raise ValueError(f'unreadable queue membership {path}: {err}') from err
    workspace = membership.get('workspace') if isinstance(membership, dict) else None
    if not isinstance(membership, dict) or (workspace is not None and (not isinstance(workspace, str) or not workspace)):
        raise ValueError(f'invalid queue membership {path}')
    return workspace or repo.split('/')[-1]


def filled_rows(text: str) -> list[str]:
    return [row.rstrip() for row in (text or '').splitlines() if row.strip()][-limits.WINDOW:]


def helper_untouched(marker_rows: list[str], current_rows: list[str]) -> bool:
    """True only when the pane shows exactly what the helper left when it wrote its marker. A direct
    mode helper prints nothing after that and cannot be typed into, so anything else - a prompt, a
    typed command, more output - means the pane is not the finished helper's."""
    return bool(marker_rows) and current_rows == marker_rows


# --- relay pointers left in a composer (#96) ----------------------------------------------------

def pointer_text(message: dict, agmsg: Path, hub_dir: Path) -> str:
    """The single line typed into a pane. Never the body - that stays in the file."""
    sender = message.get("from", "?")
    subject = message.get("subject", "")
    mid = message.get("id", "")
    return (f"workbench mail from {sender}: {subject} [id {mid}] - read it with: "
            f"python {agmsg} read {mid}  (AI_HUB={hub_dir})")


def relay_pointer(message: dict, agmsg: Path, hub_dir: Path) -> str:
    """Exactly what the relay types for this message, label included."""
    import peerchat
    return peerchat.compose_text(POINTER_LABEL, pointer_text(message, agmsg, hub_dir))


def mail_file(hub_dir: Path, box: str, mid: str) -> tuple[Path, bool] | None:
    """(path, unread) of one message in a box - the inbox, then read/, then archive/ - else None."""
    if not MAIL_ID_RE.fullmatch(box or '') or not MAIL_ID_RE.fullmatch(mid or ''):
        return None
    for folder, unread in (('', True), ('read', False), ('archive', False)):
        path = Path(hub_dir) / 'inbox' / box / folder / f'{mid}.md'
        if path.is_file():
            return path, unread
    return None


def agent_busy(peer, text: str) -> bool:
    """A turn is running in this agent pane: the busy half of `idle_blockers`, shared with the pointer
    rescue so the two can never disagree about it."""
    import peerchat
    return peerchat.is_busy(text) or (peer.tool == 'codex' and any('Working' in row for row in text.splitlines()[-6:]))


class PointerRescue:
    """Submit a relay pointer that is still sitting in an idle composer (#96).

    peerchat verifies a ring's submit inside one send; once that returns, wrongly or after its retries
    ran out, nothing looks again, and the pointer can sit typed-but-unsent until a human presses
    Enter. Each `step(texts)` looks at every agent pane: a composer whose content `owns` the exact
    pointer the relay types for mail still UNREAD in that box, seen the same on two consecutive steps
    (never racing a send or the agent itself), on an idle pane with no dialog, gets one more submit
    key through `peerchat.resubmit`. Each attempt that does not submit counts; from RESCUE_ATTEMPTS on
    it logs UNSUBMITTED and alerts (at most every ALERT_EVERY seconds per pointer). The alert is the
    rescue's own: once that pointer is rescued, or is no longer in a composer it looked at, it calls
    `resolved(peer, mid)` so the caller can take the alert back. Only ever the submit key, one per
    pointer per step, never text. The counts live in memory; a restart starts them over. The relay
    runs it on its own clock, watching and draining, and in its close loop; the conductor's close
    backstop runs it through `Closer.rescue_pointers`. In the close, `skip` leaves alone the mail the
    close does not wait for: `Closer.clear_stale_pointers` clears those pointers instead.
    """

    def __init__(self, peers, *, unread_message: Callable[[str, str], dict | None], pointer: Callable[[dict], str],
                 log: Callable[[str], None], alert: Callable[[object, str, str], None],
                 resolved: Callable[[object, str], None] | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.peers = peers
        self.unread_message = unread_message
        self.pointer = pointer
        self.log = log
        self.alert = alert
        self.resolved = resolved
        self.clock = clock
        self.last: dict[str, tuple[str, str]] = {}            # box -> (mid, content) seen on the last step
        self.attempts: dict[tuple[str, str], int] = {}
        self.alerted: dict[tuple[str, str], float] = {}

    def step(self, texts: dict, skip: Callable[[object, dict], bool] | None = None) -> list[tuple[object, str]]:
        """One look at every pane; returns (peer, mid) for each pointer it got submitted. `skip(peer,
        message)` true leaves that pointer alone."""
        import peerchat
        rescued = []
        seen: dict[str, tuple[str, str]] = {}
        looked: set[str] = set()
        for peer in self.peers:
            text = texts.get(peer.box)
            if not isinstance(text, str) or peer.tool not in peerchat.PROFILES:
                continue
            looked.add(peer.box)
            if agent_busy(peer, text) or peerchat.dialog_visible(text):
                continue
            content = peerchat.composer_content(peer.tool, text)
            mid = peerchat.pointer_id(content)
            message = self.unread_message(peer.box, mid) if mid else None
            if message is None or (skip is not None and skip(peer, message)):
                continue
            typed = self.pointer(message)
            if not peerchat.owns(content, typed):
                continue
            seen[peer.box] = (mid, content)
            if self.last.get(peer.box) != (mid, content):
                continue
            key = (peer.box, mid)
            try:
                outcome = peerchat.resubmit(peer.pane, peerchat.PROFILES[peer.tool], typed)
            except (peerchat.Refused, peerchat.Failed, agw.CtlError, OSError) as err:
                count = self.attempts[key] = self.attempts.get(key, 0) + 1
                if count < RESCUE_ATTEMPTS:
                    self.log(f"unsent pointer in {peer.box} for {mid}: rescue attempt {count} did not submit: {err}")
                    continue
                self.log(f"UNSUBMITTED pointer for {mid} in {peer.box}: {err}")
                last = self.alerted.get(key)
                if last is None or self.clock() - last >= ALERT_EVERY:
                    self.alerted[key] = self.clock()
                    self.alert(peer, mid, f"pointer typed but not submitted after {count} attempts: {err}")
                continue
            seen.pop(peer.box)
            self.forget(peer, mid)
            self.log(f"rescued unsent pointer in {peer.box} for {mid} [{outcome}]")
            rescued.append((peer, mid))
        for box, mid in list(self.attempts):
            if box in looked and seen.get(box, (None,))[0] != mid:
                # Gone from the composer it was stuck in: sent by hand, read, cleared or replaced.
                self.forget(next(peer for peer in self.peers if peer.box == box), mid)
        self.last = seen
        return rescued

    def forget(self, peer, mid: str) -> None:
        key = (peer.box, mid)
        self.attempts.pop(key, None)
        if self.alerted.pop(key, None) is not None and self.resolved is not None:
            self.resolved(peer, mid)


class Closer:
    def __init__(self, hub_dir: Path, repo: str, issue: str | int | None, peers, *, log: Callable[[str], None],
                 clock: Callable[[], float] = time.monotonic, dry_run: bool = False, workspace: str | None = None):
        self.hub_dir = Path(hub_dir)
        self.repo = repo
        self.issue = str(issue) if issue else None
        # The conductor passes its queue's workspace; the relay reads its checkout's (#66).
        self.workspace_name = workspace or workspace_of(self.hub_dir, repo)
        self.peers = peers
        self.echo = log
        self.clock = clock
        self.dry_run = dry_run
        self.settled: dict[str, tuple[str, float]] = {}
        self.decided: dict[str, str] = {}       # helper session id -> last logged decision
        self.hard: list[str] = []               # the last agent_blockers(): what a timeout never overrides
        self.soft: list[str] = []               # ... and the unread implementer mail ids it does (#44)
        self.rescue: PointerRescue | None = None  # the conductor's pointer rescue (#96), made on first use
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

    def start_cleanup(self, pr: int | None) -> None:
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

    def overdue_ok(self) -> bool:
        """After CLOSE_WAIT, when the last agent_blockers() found only unread implementer mail: close
        anyway (#44), and say so. Anything else still blocking keeps the refusal."""
        if not (self.timed_out() and not self.hard and self.soft):
            return False
        self.log(f"closing after {CLOSE_WAIT:.0f}s despite unread implementer mail: {', '.join(self.soft)}")
        return True

    def merge_time(self, number: int | None) -> datetime | None:
        """The merge or no-PR done time saved with the pending close; None when unknown."""
        try:
            state = json.loads((self.hub_dir / 'state' / 'relay.json').read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            return None
        if not isinstance(state, dict) or state.get('close_pending') != pending_key(number):
            return None
        return parse_time(state.get('close_merged_at'))

    def issue_closed(self, gh) -> tuple[bool | None, str]:
        """A fresh, fail-closed GitHub check shared by relay and conductor."""
        if not self.issue:
            return None, 'issue number unknown'
        try:
            issue = gh('issue', 'view', str(self.issue), '--repo', self.repo, '--json', 'state')
            if not isinstance(issue, dict) or issue.get('state') not in ('OPEN', 'CLOSED'):
                return None, 'invalid issue state'
            return issue['state'] == 'CLOSED', issue['state']
        except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as err:
            return None, str(err)

    def open_prs(self, gh, branch: str) -> tuple[bool | None, str]:
        """Whether this branch has an open PR; an unreadable list is unknown, never empty."""
        if not branch or issue_from_branch(branch) != self.issue:
            return None, 'issue branch unavailable'
        try:
            prs = gh('pr', 'list', '--repo', self.repo, '--head', branch,
                     '--state', 'open', '--json', 'number')
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as err:
            return None, f'open PR list unavailable: {err}'
        if not isinstance(prs, list) or not all(isinstance(pr, dict) and type(pr.get('number')) is int for pr in prs):
            return None, 'open PR list unavailable'
        return bool(prs), f'an open PR exists for {branch}' if prs else 'no open PR'

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
        name = re.compile(rf'#{self.issue} ({HELPER_NAMES})')
        return [session for workspace, session in agw.sessions(snapshot)
                if (workspace.get('name') or '').casefold() == self.workspace_name.casefold()
                and name.fullmatch(session.get('name') or '')]

    def marker_path(self, pane: str) -> Path:
        return self.hub_dir / 'state' / 'helpers' / f'{pane}.done'

    def step_helpers(self, gate: Callable[[], bool] | None = None) -> list[str]:
        """One look at every helper; closes those proven done and untouched. Returns what stays open."""
        left_open = []
        snapshot = agw.tree()
        for session in self.helper_sessions(snapshot):
            panes = agw.panes_of(session)
            reason = self.helper_reason(panes, session)
            label = f"{session.get('name')} ({session.get('id')})"
            if reason:
                left_open.append(f"{session.get('name')} ({reason})")
                self._decide(session.get('id'), f"helper {label} stays open: {reason}")
                continue
            if not self.autonomous():
                self.log(f"NOT closing helper {label}: autonomy was turned off")
                left_open.append(f"{session.get('name')} (autonomy off)")
                continue
            if self.dry_run:
                self.log(f"[dry-run] would close helper {label}")
                continue
            if gate is not None and not gate():
                self.log(f"helper {label} stays open: issue not verified CLOSED")
                left_open.append(f"{session.get('name')} (issue not verified CLOSED)")
                continue
            self._close_helper(session, panes, f"closing helper {label}: done and untouched")
        return left_open

    def _close_helper(self, session: dict, panes: list[str], why: str) -> None:
        self.log(why)
        for pane in panes:
            agw.clear_restore(pane)
        agw.close_session(session.get('id'))
        self.marker_path(panes[0]).unlink(missing_ok=True)

    def _decide(self, session_id: str, text: str) -> None:
        """Log a helper's decision once, until it changes."""
        if self.decided.get(session_id) != text:
            self.decided[session_id] = text
            self.log(text)

    def result_mail_unread(self, marker: dict) -> str | None:
        """None once the result mail the marker names has been read (moved to `read/` or `archive/`
        of its box); else why the helper stays. A marker naming no mail, or a mail found nowhere,
        keeps it: without that proof nobody is known to have seen the result."""
        mail, box = marker.get('mail'), marker.get('to')
        if not isinstance(mail, str) or not MAIL_ID_RE.fullmatch(mail):
            return 'its marker names no result mail (a helper from before #84, a failed round or tool error, or its post failed)'
        import hub
        if not isinstance(box, str) or not hub.BOX_RE.match(box):
            return f'its marker names no valid mailbox for its result mail {mail}'
        folder = self.hub_dir / 'inbox' / box
        if (folder / f'{mail}.md').exists():
            return f'its result mail {mail} is unread in {box}'
        if any((folder / sub / f'{mail}.md').exists() for sub in ('read', 'archive')):
            return None
        return f'its result mail {mail} is not in the {box} mailbox'

    def step_finished_helpers(self) -> list[str]:
        """One look at every helper (#84): closes a revmux or suite helper whose completion marker
        exists and names a result mail that has been read. Whatever its pane shows - no settle, no
        rows compared. Never the human's revdiff, never a split session. Returns the names it closed."""
        closed = []
        for session in self.helper_sessions(agw.tree()):
            session_id = session.get('id')
            name = session.get('name') or ''
            label = f"{name} ({session_id})"
            panes = agw.panes_of(session)
            reason, marker = None, None
            if name.endswith(' your review'):
                reason = "the human's revdiff closes only with the human or the loop's end"
            elif len(panes) != 1:
                # Identity, not idleness: a pane someone split beside the helper must not go with it.
                reason = 'not a single-pane helper'
            else:
                try:
                    marker = json.loads(self.marker_path(panes[0]).read_text(encoding='utf-8-sig'))
                except FileNotFoundError:
                    reason = 'no completion marker (still running, or ended without one)'
                except (OSError, ValueError):
                    reason = 'unreadable completion marker'
            if reason is None:
                if not isinstance(marker, dict) or marker.get('kind') not in ('revmux', 'suite'):
                    reason = 'not a revmux or suite helper'
                else:
                    reason = self.result_mail_unread(marker)
            if reason:
                self._decide(session_id, f"helper {label} stays open: {reason}")
                continue
            if self.dry_run:
                self._decide(session_id, f"[dry-run] would close finished helper {label}")
                continue
            self._close_helper(session, panes, f"closing finished helper {label}: result mail {marker['mail']} read")
            self.decided.pop(session_id, None)
            closed.append(name)
        return closed

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
    def no_pr_done_record(self) -> dict | None:
        try:
            done = json.loads((self.hub_dir / 'state' / 'loop-done.json').read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            return None
        return done if (isinstance(done, dict) and done.get('noPr') is True and done.get('pr') is None
                        and str(done.get('issue')) == str(self.issue)) else None

    def no_pr_done(self) -> bool:
        return self.no_pr_done_record() is not None

    def agent_blockers(self, number: int | None) -> list[str]:
        """What still stops the issue-session close. Empty only when the loop is provably over. Keeps
        the split for overdue_ok(): `hard` (every other reason) and `soft` (unread implementer mail ids)."""
        import hub
        hard: list[str] = []
        soft: list[str] = []
        try:
            done = json.loads((self.hub_dir / 'state' / 'loop-done.json').read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            done = {}
        if number is None:
            if not self.no_pr_done():
                hard.append('the planner has not recorded `wb.py loop-state done --no-pr`')
        elif not isinstance(done, dict) or done.get('pr') != number or done.get('noPr') is True:
            hard.append(f'the planner has not recorded `wb.py loop-state done --pr {number}`')
        inbox = self.hub_dir / 'inbox'
        for path in sorted((inbox / 'claude').glob('*.md')):
            # Anything the planner has not read - above all a human's "don't close" - stops the close.
            hard.append(f'the planner has unread mail {path.stem}')
        merged_at = self.merge_time(number)
        ignored = []
        for path in sorted((inbox / 'codex').glob('*.md')):
            try:
                message = hub.parse_message(path)
            except (OSError, ValueError):
                message = {}
            if message.get('from') not in (None, 'claude', 'human', 'github'):
                continue
            if self.ignored_implementer_mail(number, path.stem, message, merged_at):
                # The relay's final PR notice, or mail after the merge/no-PR done time: no action needed.
                ignored.append(path.stem)
                continue
            soft.append(path.stem)
        if ignored and self.decided.get('__ignored__') != ' '.join(ignored):
            self.decided['__ignored__'] = ' '.join(ignored)
            self.log(f"ignoring unread post-merge mail for the implementer: {', '.join(ignored)}")
        if (self.hub_dir.parent / '.git' / 'index.lock').exists():
            hard.append('.git/index.lock exists')
        for peer in self.peers:
            try:
                text = agw.pane_text(peer.pane)
            except (agw.CtlError, OSError) as err:
                hard.append(f'{peer.box} pane unreadable: {err}')
                continue
            state = self.settle(peer.pane, text)
            if state:
                hard.append(f'{peer.box} pane {state}')
            hard += idle_blockers(peer, text)
        self.hard, self.soft = hard, soft
        return hard + [f'the implementer has not read {mid}' for mid in soft]

    @staticmethod
    def ignored_implementer_mail(number: int | None, stem: str, message: dict, merged_at: datetime | None) -> bool:
        """Implementer mail the close does not wait for (#44): from a sender it does not count (a helper),
        the relay's final notice for this PR, or anything created at or after the merge/no-PR done time."""
        sender = message.get('from')
        if sender not in (None, 'claude', 'human', 'github'):
            return True
        created = parse_time(message.get('created'))
        return ((number is not None and sender == 'github' and stem.startswith(f'github-pr{number}-'))
                or (merged_at is not None and created is not None and created >= merged_at))

    def needs_no_reading(self, number: int | None, peer, message: dict) -> bool:
        """An unread message whose relay pointer the close clears instead of ringing (#96): implementer
        mail the close does not wait for. Planner mail always needs reading."""
        return peer.box == 'codex' and self.ignored_implementer_mail(
            number, message.get('id', ''), message, self.merge_time(number))

    # --- relay pointers left in a composer (#96) --------------------------------------------------
    def unread_message(self, box: str, mid: str) -> dict | None:
        import hub
        found = mail_file(self.hub_dir, box, mid)
        if found is None or not found[1]:
            return None
        try:
            return hub.parse_message(found[0])
        except (OSError, ValueError):
            return None

    def pointer_alert(self, peer, mid: str, reason: str) -> None:
        message = f"workbench mail for {peer.box} ({mid}) is waiting: {reason}"
        self.log(f"ALERT {message}")
        if self.dry_run:
            return
        try:
            agw.set_status('blocked', sound=True, blink=True, pane_id=peer.pane)
        except (agw.CtlError, OSError) as err:
            self.log(f"could not set blocked status for {peer.box}: {err}")
        try:
            agw.notify(peer.pane, message, title='workbench close')
        except (agw.CtlError, OSError) as err:
            self.log(f"could not notify {peer.box}: {err}")

    def pointer_resolved(self, peer, mid: str) -> None:
        if self.dry_run:
            return
        try:
            agw.set_status('idle', pane_id=peer.pane)
        except (agw.CtlError, OSError) as err:
            self.log(f"could not clear the blocked status for {peer.box}: {err}")

    def rescue_pointers(self, number: int | None) -> list:
        """One `PointerRescue` step over the agent panes, for the conductor's backstop (the relay runs
        its own, which also records the mail as announced). Mail the close does not wait for is left
        to `clear_stale_pointers`."""
        if self.dry_run:
            return []
        if self.rescue is None:
            self.rescue = PointerRescue(self.peers, unread_message=self.unread_message,
                                        pointer=lambda message: relay_pointer(message, AGMSG, self.hub_dir),
                                        log=self.log, alert=self.pointer_alert, resolved=self.pointer_resolved,
                                        clock=self.clock)
        texts = {}
        for peer in self.peers:
            try:
                texts[peer.box] = agw.pane_text(peer.pane)
            except (agw.CtlError, OSError) as err:
                texts[peer.box] = err
        return self.rescue.step(texts, skip=lambda peer, message: self.needs_no_reading(number, peer, message))

    def clear_stale_pointers(self, number: int | None) -> list:
        """Delete a relay pointer that no longer needs reading from a settled, idle composer (#96).

        Its mail is read or archived, or is implementer mail the close does not wait for (the relay's
        final notice, post-merge mail, a helper's): ringing it now would only start a turn the close
        has to wait out, while leaving it makes the composer not provably empty and refuses the close.
        The pane must be settled by `agent_blockers`' own record (not reset here), idle, with no dialog,
        and its composer must own exactly the pointer the relay types for that mail. Ctrl+U through
        `peerchat.clear_pointer`; a composer it cannot empty stays a blocker, as before. Pointers for
        mail still to be read are `rescue_pointers`' (and the relay's), never cleared. Returns (peer, mid)
        for each pointer it cleared, so the relay can count that mail as rung."""
        import hub
        import peerchat
        merged_at = self.merge_time(number)
        cleared_pointers = []
        for peer in self.peers:
            if peer.tool not in peerchat.PROFILES:
                continue
            try:
                text = agw.pane_text(peer.pane)
            except (agw.CtlError, OSError):
                continue
            seen = self.settled.get(peer.pane)
            if (seen is None or seen[0] != limits.tail_hash(text) or self.clock() - seen[1] < CLOSE_SETTLE
                    or agent_busy(peer, text) or peerchat.dialog_visible(text)):
                continue
            content = peerchat.composer_content(peer.tool, text)
            mid = peerchat.pointer_id(content)
            found = mail_file(self.hub_dir, peer.box, mid) if mid else None
            if found is None:
                continue
            path, unread = found
            try:
                message = hub.parse_message(path)
            except (OSError, ValueError):
                continue
            if unread and not (peer.box == 'codex' and self.ignored_implementer_mail(number, path.stem, message, merged_at)):
                continue
            typed = relay_pointer(message, AGMSG, self.hub_dir)
            if not peerchat.owns(content, typed):
                continue
            where = f"a stale relay pointer for {mid} from {peer.box}'s composer"
            if self.dry_run:
                self.log(f"[dry-run] would clear {where}")
                continue
            self.log(f"clearing {where}")
            try:
                cleared = peerchat.clear_pointer(peer.pane, peerchat.PROFILES[peer.tool], typed)
            except (peerchat.Refused, agw.CtlError, OSError) as err:
                self.log(f"could not clear {where}: {err}")
                continue
            if cleared:
                self.log(f"cleared {where}")
                cleared_pointers.append((peer, mid))
            else:
                self.log(f"could not clear {where}: not empty after {peerchat.CLEAR_PRESSES} presses")
        return cleared_pointers

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


def idle_blockers(peer, text: str) -> list[str]:
    """Why an agent pane is not provably idle: a turn is running, or its composer is not provably
    empty. Empty only when it is idle. Shared by the close and the relay's stall watch (#45), so the
    two can never disagree about what idle means."""
    import peerchat
    reasons = []
    if agent_busy(peer, text):
        reasons.append(f'{peer.box} is running a turn')
    profile = peerchat.PROFILES[peer.tool]
    content = peerchat.composer_content(peer.tool, text)
    if content is None or not peerchat.looks_empty(profile, content):
        reason = f'{peer.box} composer is not provably empty'
        if peer.tool == 'claude':
            reason += (' (a greyed prompt suggestion? agents the workbench launches have them off;'
                       ' for an adopted Claude set "promptSuggestionEnabled": false in ~/.claude/settings.json)')
        reasons.append(reason)
    return reasons


def relay_alive(repo: str, issue: str, snapshot, workspace: str | None = None) -> bool:
    """Is this issue's relay session (`#N relay` in its workspace: the queue's, #66, else the repo's) in the tree?"""
    workspace_name = (workspace or repo.split('/')[-1]).casefold()
    return any((workspace.get('name') or '').casefold() == workspace_name and session.get('name') == f'#{issue} relay'
               for workspace, session in agw.sessions(snapshot))
