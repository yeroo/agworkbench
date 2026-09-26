#!/usr/bin/env python3
"""relay - the workbench's doorbell and its eye on GitHub.

Four jobs, one loop, one process per issue, running in its own visible agwinterm session:

1. **Mail.** Agents never type into each other's panes. They write a message file into the
   workbench mailbox (`agmsg send`, no `--nudge`), and the relay types a one-line pointer into the
   recipient's pane. This is what lets Codex stay sandboxed: its sandbox denies the agwinterm control
   pipe, so it cannot ring a doorbell itself - but it can write a file inside its own workspace.

2. **The pull request.** Once a PR exists for the issue branch, the relay watches it and files a
   message to Claude on every event that needs acting on: a new review, a changed review decision,
   a new comment, a merge, a close. After merge/closure the loop drains its final notices, with
   a bounded wait for recipients whose composers are unavailable.
   It follows the newest OPEN PR from this repository, and catches PRs created and finished
   after the saved server-time watch boundary. Older PRs first seen finished are ignored;
   observation survives restarts, and a fully drained PR is retired so a later run can continue.

3. **Usage limits (#24).** Every `--limit-interval` seconds it reads both agent panes and asks
   `limits.classify` whether the agent there has hit its usage limit. An episode seen on two
   consecutive reads is mailed to the planner once (sender `relay`), with a blocked status and a
   notification; mail to a limited implementer is held until the planner fails it over. Checks
   stop once the PR is finished.

4. **The close after merge (#27, #33).** On an autonomous checkout, after a MERGED PR's final
   notices are delivered, it runs closer.py while it keeps delivering mail. Helper sessions close
   first, each on its own evidence (its completion marker, an ended direct-mode pane showing exactly
   the marker's rows), whatever the agents are doing. The gates - the planner has recorded
   `loop-state done`, no mail is unread, both agent panes are provably idle - apply to the issue
   session and the relay's own session only. Every step goes to `.workbench/state/relay-close.log`;
   a stop request, unread mail to the planner (a human's above all) or autonomy turned off stops
   it. It never closes on a timeout
   alone: mail the implementer need not act on (its final notices, anything sent after the merge)
   is ignored, and after the wait only other unread implementer mail is overridden (#44).
   A pending close survives a restart (`close_pending`, with the merge time `close_merged_at`). In
   queue mode a close that gives up is handed to the conductor (#44): the relay keeps
   `close_pending`, records `close_handoff` and closes its own session, so the conductor's backstop
   retries it. No doorbell is lost: the relay exits after a close either way.

Nothing here polls on behalf of an agent: agents are woken by the relay and otherwise idle. The
relay itself polls the mailbox directory, the GitHub API and the two agent panes (for limits),
none of which can push to it.

Typing into a pane goes through `peerchat` (vendored, fail-closed): a composer that is not
provably empty, a dialog on screen, or a pane that is not an agent is a refusal, never a send. A
refusal before typing is retried on the next tick. Submit keys are verified and retried by
peerchat; a failed ring is announced only after a later send succeeds from an empty composer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from peerchat import is_busy  # noqa: E402 - also available as relay.is_busy
import limits  # noqa: E402
import closer  # noqa: E402

PR_FIELDS = "number,url,state,createdAt,updatedAt,closedAt,reviewDecision,mergedAt,reviews,comments,headRefName,isCrossRepository"
HOLD_ALERT_AFTER = 60.0
AMBIGUOUS_ALERT_AFTER = 600.0
ALERT_EVERY = 300.0
TERMINAL_DRAIN_TIMEOUT = 30 * 60.0
LIMIT_READS = 2          # consecutive reads that start (or end) a usage-limit episode


@dataclass(frozen=True)
class Peer:
    box: str       # mailbox name: claude | codex
    tool: str      # peerchat profile: claude | codex
    pane: str      # agwinterm pane id


# --- pure logic (tested without a terminal or a network) ------------------------------------

def now() -> float:
    return time.monotonic()


def pause(seconds: float) -> None:
    time.sleep(seconds)


def wall() -> float:
    """Wall-clock seconds, for timestamps that must survive a restart (monotonic ones do not)."""
    return time.time()


@dataclass
class Hold:
    first_at: float
    reason: str
    last_alert_at: float | None = None
    clear_pending: bool = False
    condition_since: float | None = None
    ambiguous_text: str | None = None

    @property
    def alerted(self) -> bool:
        return self.last_alert_at is not None


def pointer_text(message: dict[str, Any], agmsg: Path, hub_dir: Path) -> str:
    """The single line typed into a pane. Never the body - that stays in the file."""
    sender = message.get("from", "?")
    subject = message.get("subject", "")
    mid = message.get("id", "")
    return (f"workbench mail from {sender}: {subject} [id {mid}] - read it with: "
            f"python {agmsg} read {mid}  (AI_HUB={hub_dir})")


def pr_events(old: dict[str, Any] | None, new: dict[str, Any] | None) -> list[dict[str, Any]]:
    """What changed on the PR between two snapshots, as messages worth filing to Claude.

    A snapshot is the `gh pr view --json` object plus `inline`, the list of line comments. Only
    changes produce events: the relay can restart and re-read the same PR without re-announcing it.
    """
    events: list[dict[str, Any]] = []
    if new is None:
        return events
    number = new.get("number")
    if old is not None and old.get('number') != number:
        old = None
    if old is None or (new.get('state') == 'OPEN' and old.get('state') in ('MERGED', 'CLOSED')):
        if new.get('state') != 'OPEN':
            return events
        events.append({"kind": "note", "identity": ['open', new.get('openingAt', new.get('createdAt'))],
                       "subject": f"PR #{number} is open",
                       "body": f"{new.get('url')}\n\nThe relay is now watching it for reviews, comments "
                               f"and the merge."})
        old = {"reviews": [], "comments": [], "inline": [], "reviewDecision": "", "state": "OPEN"}

    seen_reviews = {r.get("id") for r in old.get("reviews") or []}
    for review in new.get("reviews") or []:
        if review.get("id") in seen_reviews:
            continue
        author = (review.get("author") or {}).get("login", "?")
        state = review.get("state", "")
        body = (review.get("body") or "").strip() or "(no summary text)"
        events.append({"kind": "review", "subject": f"PR #{number}: review from {author} - {state}",
                       "body": body, "identity": ['review', review.get('id') or review]})

    seen_comments = {c.get("id") for c in old.get("comments") or []}
    for comment in new.get("comments") or []:
        if comment.get("id") in seen_comments:
            continue
        author = (comment.get("author") or {}).get("login", "?")
        events.append({"kind": "message", "subject": f"PR #{number}: comment from {author}",
                       "body": (comment.get("body") or "").strip(),
                       "identity": ['comment', comment.get('id') or comment]})

    seen_inline = {c.get("id") for c in old.get("inline") or []}
    for comment in new.get("inline") or []:
        if comment.get("id") in seen_inline:
            continue
        author = (comment.get("user") or {}).get("login", "?")
        where = f"{comment.get('path')}:{comment.get('line') or comment.get('original_line') or '?'}"
        events.append({"kind": "review", "subject": f"PR #{number}: line comment from {author} on {where}",
                       "body": (comment.get("body") or "").strip(),
                       "identity": ['inline', comment.get('id') or comment]})

    if new.get("reviewDecision") and new.get("reviewDecision") != old.get("reviewDecision"):
        events.append({"kind": "note", "subject": f"PR #{number}: review decision is now "
                                                   f"{new['reviewDecision']}", "body": new.get("url", ""),
                       "identity": ['decision', old.get('reviewDecision'), new['reviewDecision'], new.get('updatedAt'),
                                    sorted(r.get('id', '') for r in new.get('reviews') or [])]})

    state = new.get("state")
    if state != old.get("state"):
        if state == "MERGED":
            events.append({"kind": "note", "subject": f"PR #{number} MERGED - the loop is complete",
                           "body": f"Merged at {new.get('mergedAt')}. {new.get('url')}", "terminal": True,
                           "identity": ['MERGED', new.get('mergedAt')]})
        elif state == "CLOSED":
            events.append({"kind": "note", "subject": f"PR #{number} was CLOSED without merging",
                           "body": new.get("url", ""), "terminal": True,
                           "identity": ['CLOSED', new.get('closedAt')]})
    return events


def fast_terminal_events(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Discover a PR and its existing discussion, then announce its terminal transition."""
    opened = dict(snapshot, state='OPEN')
    return pr_events(None, opened) + pr_events(opened, snapshot)


def event_message_id(number: int, event: dict[str, Any], box: str) -> str:
    identity = json.dumps([number, event['kind'], event['identity'], box], sort_keys=True)
    digest = hashlib.sha256(identity.encode('utf-8')).hexdigest()[:8]
    return f'github-pr{number}-{event["kind"]}-{digest}-{box}'


timestamp = closer.parse_time


def github_time() -> datetime | None:
    """Use GitHub's response clock; never substitute the workstation's wall clock."""
    try:
        done = subprocess.run(['gh', 'api', '-i', 'rate_limit'], capture_output=True, text=True,
                              encoding='utf-8', errors='replace', timeout=10)
        if done.returncode:
            return None
        for line in done.stdout.splitlines():
            if not line.strip():
                break  # Only the response headers, not JSON fields in the body.
            name, separator, value = line.partition(':')
            if separator and name.casefold() == 'date':
                parsed = parsedate_to_datetime(value.strip())
                if parsed.tzinfo is not None:
                    return parsed.astimezone(timezone.utc).replace(microsecond=0)
    except (OSError, ValueError, TypeError, OverflowError, subprocess.SubprocessError):
        pass
    return None


PANE_ID_RE = __import__("re").compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def check_panes(claude: str, codex: str) -> None:
    """Refuse to start on pane ids that cannot be right. The first live run passed "4" as Claude's
    pane and Claude's own pane as Codex's; only the composer check stopped mail going to the
    wrong agent. A relay that cannot be pointed at the wrong pane is better than one that notices."""
    for name, pane in (("claude", claude), ("codex", codex)):
        if not PANE_ID_RE.match(pane or ""):
            raise SystemExit(f"relay: --{name}-pane {pane!r} is not a pane id")
    if claude == codex:
        raise SystemExit("relay: Claude and Codex cannot share a pane")


def same_repo(candidate: dict[str, Any], repo: str) -> bool:
    head_repo = (candidate.get('head') or {}).get('repo') or {}
    return head_repo.get('full_name', '').casefold() == repo.casefold()


def select_open(candidates: list[dict[str, Any]], repo: str) -> dict[str, Any] | None:
    return max((pr for pr in candidates if pr.get('state') == 'open' and same_repo(pr, repo)),
               key=lambda pr: pr['number'], default=None)


def gh_json(args: list[str]) -> Any:
    """A failed lookup is unknown, never evidence that the open list is empty."""
    try:
        done = subprocess.run(['gh', *args], capture_output=True, text=True,
                              encoding='utf-8', errors='replace')
        return json.loads(done.stdout) if done.returncode == 0 else None
    except (OSError, ValueError):
        return None


def gh_pages(endpoint: str) -> list[dict[str, Any]] | None:
    pages = gh_json(['api', endpoint, '--paginate', '--slurp'])
    if not isinstance(pages, list) or not all(isinstance(page, list) for page in pages):
        return None
    return [item for page in pages for item in page]


# --- effects ---------------------------------------------------------------------------------

@dataclass
class PrFetch:
    snapshot: dict[str, Any]
    fast: bool = False
    previous: dict[str, Any] | None = None


class Relay:
    def __init__(self, hub_dir: Path, peers: list[Peer], repo: str, branch: str,
                 mail_interval: float, pr_interval: float, dry_run: bool = False,
                 limit_interval: float = 30.0):
        os.environ["AI_HUB"] = str(hub_dir)
        import hub  # noqa: E402 - imported after AI_HUB is set so its paths point at this workbench
        hub.reload_paths()
        self.hub = hub
        self.hub_dir = hub_dir
        self.peers = peers
        self.repo = repo
        self.branch = branch
        self.mail_interval = mail_interval
        self.pr_interval = pr_interval
        self.limit_interval = limit_interval
        # Limit rows already on screen when this relay started (e.g. the old tool's message above
        # the agent that replaced it): ignored until they leave the pane's tail.
        self.limit_baseline: dict[str, set[str]] = {}
        # True once the PR is finished: limit checks stop, so no limit may hold the final notices.
        self.draining = False
        self.dry_run = dry_run
        self.state_file = hub_dir / "state" / "relay.json"
        self.stop_file = hub_dir / "state" / "relay.stop"
        self.state = self._load()
        saved_pr = self.state.get('pr')
        saved_branch = self.state.get('branch', (saved_pr or {}).get('headRefName'))
        changed = self.state.get('branch') != self.branch
        if saved_branch != self.branch or (saved_pr and saved_pr.get('headRefName') != self.branch):
            branch_keys = ('pr', 'terminal_mail', 'ignored_prs', 'seen_open', 'completed_prs',
                           'watch_since', 'outbox', 'event_sequence')
            if saved_branch is not None or any(self.state.get(key) for key in branch_keys):
                stale_branch = saved_pr.get('headRefName') if saved_pr else saved_branch
                self.log(f"discarding saved PR state for branch {stale_branch} "
                         f"(this relay watches {self.branch})")
            for key in branch_keys:
                self.state.pop(key, None)
            saved_pr = None
            changed = True
        self.state['branch'] = self.branch
        self._dry_watch_since = None
        if saved_pr and saved_pr.get('state') == 'OPEN' and 'seen_open' not in self.state:
            self.state['seen_open'] = [saved_pr['number']]
            changed = True
        tools = {peer.box: peer.tool for peer in peers}
        for box, episode in list(self.state.get('limits', {}).items()):
            if tools.get(box) != episode.get('tool'):
                # The box now runs another tool: the failover this episode asked for happened.
                self.state['limits'].pop(box)
                changed = True
        if 'limits' in self.state and not self.state['limits']:
            self.state.pop('limits')
        if changed and not self.dry_run:
            self._save()
        self.agmsg = HERE / "agmsg.py"
        self.holds: dict[tuple[str, str], Hold] = {}
        for box in self.state.get('reset_pending', []):
            if box in {peer.box for peer in peers}:
                self.holds[(box, '')] = Hold(now(), 'status reset pending from previous relay',
                                            last_alert_at=now(), clear_pending=True)
        # Old terminal notices may have been filed by the preexisting-PR bug. Preserve pending
        # delivery for compatibility, but never infer OPEN observation from those notices.
        if (saved_pr and saved_pr.get('state') in ('MERGED', 'CLOSED')
                and not self.state.get('outbox')
                and not self.pending_terminal_mail() and not self.state.get('reset_pending')):
            # A restart after a complete drain: retire() records the pending close (#27) for run().
            self.retire(saved_pr['number'])

    def _load(self) -> dict[str, Any]:
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"announced": [], "pr": None}

    def _save(self) -> None:
        self.state['branch'] = self.branch
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_file)

    def log(self, text: str) -> None:
        print(f"{time.strftime('%H:%M:%S')} {text}", flush=True)

    # mail -----------------------------------------------------------------------------------
    def hold(self, peer: Peer, mid: str, reason: str, *, failed: bool = False,
             ambiguous_text: str | None = None) -> None:
        instant = now()
        entry = self.holds.setdefault((peer.box, mid), Hold(instant, reason))
        if entry.ambiguous_text != ambiguous_text:
            # Keep alert ownership and total hold duration. Only ambiguity transitions
            # restart the condition timer; ordinary changes of reason retain it.
            entry.condition_since = instant
            entry.ambiguous_text = ambiguous_text
        entry.reason = reason
        if failed:
            self.log(f"FAILED ringing {peer.box} for {mid}: {reason}")
        else:
            self.log(f"{peer.box} not ready ({reason}); holding {mid}")
        since = entry.condition_since if entry.condition_since is not None else entry.first_at
        threshold = AMBIGUOUS_ALERT_AFTER if ambiguous_text is not None else HOLD_ALERT_AFTER
        if (failed or instant - since >= threshold) and (
                entry.last_alert_at is None or instant - entry.last_alert_at >= ALERT_EVERY):
            if not self.dry_run:
                entry.last_alert_at = instant
                self.alert(peer, mid, entry.reason)

    def alert(self, peer: Peer, mid: str, reason: str) -> None:
        import agw
        message = f"workbench mail for {peer.box} ({mid}) is waiting: {reason}"
        self.log(f"ALERT {message}")
        # Attempt the notification even if setting status failed (and vice versa).
        try:
            agw.set_status('blocked', sound=True, blink=True, pane_id=peer.pane)
        except (agw.CtlError, OSError) as err:
            self.log(f"could not set blocked status for {peer.box}: {err}")
        try:
            agw.notify(peer.pane, message, title='workbench relay')
        except (agw.CtlError, OSError) as err:
            self.log(f"could not notify {peer.box}: {err}")

    def clear(self, peer: Peer, mid: str) -> Hold | None:
        """Clear our last alert for a recipient. Without conditional terminal status ownership,
        this requested idle reset can race a newer status set by the agent's hook."""
        import agw
        key = (peer.box, mid)
        entry = self.holds.get(key)
        if entry and entry.alerted and not self.dry_run and not any(
                box == peer.box and (box, other_mid) != key and held.alerted
                for (box, other_mid), held in self.holds.items()):
            try:
                agw.set_status('idle', pane_id=peer.pane)
            except (agw.CtlError, OSError) as err:
                entry.clear_pending = True
                pending = self.state.setdefault('reset_pending', [])
                if peer.box not in pending:
                    pending.append(peer.box)
                    self._save()
                self.log(f"could not clear relay status for {peer.box}: {err}")
                return entry
            pending = self.state.get('reset_pending', [])
            if peer.box in pending:
                pending.remove(peer.box)
                self._save()
        if entry:
            self.holds.pop(key)
            self.log(f"cleared hold {peer.box} for {mid}; last reason: {entry.reason}")
        return entry

    # usage limits (#24) -------------------------------------------------------------------------
    def check_limits(self) -> None:
        """Classify each pane; an episode seen on LIMIT_READS consecutive reads is announced once."""
        import agw
        episodes = dict(self.state.get('limits', {}))
        changed = False
        for peer in self.peers:
            try:
                text = agw.pane_text(peer.pane)
            except (agw.CtlError, OSError) as err:
                self.log(f"limit check: cannot read {peer.box}: {err}")
                continue
            found = limits.classify(text, peer.tool)
            episode = episodes.get(peer.box)
            if peer.box not in self.limit_baseline:
                continuing = bool(found and episode and episode.get('line') == found.line)
                self.limit_baseline[peer.box] = {found.line} if found and not continuing else set()
                if self.limit_baseline[peer.box]:
                    self.log(f"limit check: ignoring {peer.box}'s limit row already on screen at start: {found.line}")
            baseline = self.limit_baseline[peer.box]
            if found and found.line in baseline:
                found = None
            elif not found and baseline and not any(line in text for line in baseline):
                baseline.clear()
            if found:
                tail = limits.tail_hash(text)
                if episode and episode.get('kind') == found.kind:
                    episode['hits'] = episode.get('hits', 0) + 1
                    episode['misses'] = 0
                    if episode.get('tail') != tail:
                        episode.update(tail=tail, since=wall())
                else:
                    episode = episodes[peer.box] = {
                        'kind': found.kind, 'line': found.line, 'tool': peer.tool,
                        'firstSeen': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                        'since': wall(), 'tail': tail, 'hits': 1, 'misses': 0, 'announced': False}
                episode['exited'] = found.exited
                if episode['hits'] >= LIMIT_READS and not episode['announced']:
                    if self.dry_run:
                        self.log(f"[dry-run] would announce usage limit: {peer.box} ({peer.tool}) {found.kind}")
                    else:
                        self.announce_limit(peer, episode, text)
                        episode['announced'] = True
                changed = True
            elif episode:
                episode['misses'] = episode.get('misses', 0) + 1
                if episode['misses'] >= LIMIT_READS:
                    episodes.pop(peer.box)
                    self.log(f"usage limit episode ended for {peer.box}")
                changed = True
        if changed:
            if episodes:
                self.state['limits'] = episodes
            else:
                self.state.pop('limits', None)
            if not self.dry_run:
                self._save()

    def announce_limit(self, peer: Peer, episode: dict, text: str) -> None:
        import agw
        subject = f"usage limit: {peer.box} ({peer.tool}) {episode['kind']}"
        rows = [row for row in text.splitlines() if row.strip()][-limits.WINDOW:]
        if peer.box == 'claude':
            step = ("The planner itself is limited, so nobody can act on this mail until it can: "
                    "the human has been notified. The loop waits.")
        elif episode['kind'] == 'warning':
            step = ("The implementer shows a usage warning with a chooser. Never answer it: tell the "
                    "human in one line and set blocked (start-github-issue.md, Usage limits).")
        else:
            step = ("The implementer has hit its usage limit"
                    + (" and exited to a shell" if episode.get('exited') else "")
                    + ". Follow start-github-issue.md, Usage limits: check the frame below, then fail "
                    "over with `github-workbench.cmd <issue> -Failover` (Bash timeout 600000).")
        body = "\n".join([f"Matched: {episode['line']}", f"Pane: {peer.box} ({peer.tool}) {peer.pane}",
                           f"First seen: {episode['firstSeen']}", "", "Next step: " + step, "",
                           "Last rows of the pane:", "", "```", *rows, "```"])
        try:
            self.hub.write_message(to='claude', sender='relay', kind='note', subject=subject, body=body)
        except OSError as err:
            self.log(f"could not file usage-limit mail: {err}")
        self.log(f"ALERT {subject}: {episode['line']}")
        try:
            agw.set_status('blocked', sound=True, blink=True, pane_id=peer.pane)
        except (agw.CtlError, OSError) as err:
            self.log(f"could not set blocked status for {peer.box}: {err}")
        try:
            agw.notify(peer.pane, f"{subject}: {episode['line']}", title='workbench relay')
        except (agw.CtlError, OSError) as err:
            self.log(f"could not notify {peer.box}: {err}")

    # the autonomous close (#27) ------------------------------------------------------------------
    def closer(self) -> "closer.Closer":
        return closer.Closer(self.hub_dir, self.repo, closer.issue_from_branch(self.branch), self.peers, log=self.log, clock=now,
                             dry_run=self.dry_run)

    def finish_close(self) -> None:
        """The close is done or refused: nothing to resume on a restart."""
        popped = [self.state.pop(key, None) for key in ('close_pending', 'close_merged_at', 'close_handoff')]
        if any(value is not None for value in popped) and not self.dry_run:
            self._save()

    def conductor_running(self) -> bool:
        """Is this checkout's queue conductor running (#44)? Its worker lock is held and, read under the
        queue's state lock, its owner is `running`. Under that lock because a conductor decides to
        finish under it too, after reading every member's relay.json (conductor.handed_off): a relay
        that saved close_handoff and then sees `running` here is certain the conductor will see it."""
        import conductor
        try:
            member = json.loads((self.hub_dir / 'state' / 'queue-member.json').read_text(encoding='utf-8-sig'))
            store = conductor.Store(member['queue'])
            if not store.running():
                return False
            with conductor.Lock(store.state_lock):
                data = json.loads(store.path.read_text(encoding='utf-8-sig'))
            return (data.get('owner') or {}).get('state') == 'running'
        except (OSError, ValueError, KeyError, TypeError, AttributeError, conductor.QueueError):
            return False

    def hand_off_close(self, close: "closer.Closer", number: int, reasons: list[str]) -> bool:
        """Queue mode (#44): leave the refused close to the conductor's backstop. It keeps
        close_pending and closes this relay's session, since the conductor defers to a live one.
        False (nothing handed off) outside queue mode, when no conductor is running to take it (a
        queue that is not watching finishes once its last member has a PR), or when the session is
        not provably ours."""
        import agw
        if self.dry_run or not (self.hub_dir / 'state' / 'queue-member.json').exists():
            return False
        if not self.conductor_running():
            close.log("NOT handing the close to the queue conductor: it is not running")
            return False
        own = self.own_session(close)
        if own is None:
            close.log("NOT handing the close to the queue conductor: this relay's session is not provably its own")
            return False
        mine, session = own
        self.state['close_pending'] = number
        self.state['close_handoff'] = {'pr': number, 'at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                                       'reasons': reasons}
        self._save()
        if not self.conductor_running():
            # It finished between the two looks, before it could have seen the handoff.
            close.log("NOT handing the close to the queue conductor: it finished meanwhile")
            return False
        close.log(f"handing the close to the queue conductor; closing the relay session {session.get('id')}")
        try:
            agw.notify(mine, f"PR #{number}: autonomous close handed to the queue conductor", title='workbench relay')
        except (agw.CtlError, OSError) as err:
            self.log(f"could not notify: {err}")
        agw.clear_restore(mine)
        agw.close_session(session.get('id'))
        return True

    def close_alert(self, reason: str) -> None:
        import agw
        message = f"autonomous close stopped: {reason} (log: .workbench/state/relay-close.log)"
        if self.dry_run:
            return
        for peer in self.peers:
            if peer.box != 'claude':
                continue
            try:
                agw.set_status('blocked', sound=True, blink=True, pane_id=peer.pane)
            except (agw.CtlError, OSError) as err:
                self.log(f"could not set blocked status: {err}")
            try:
                agw.notify(peer.pane, message, title='workbench relay')
            except (agw.CtlError, OSError) as err:
                self.log(f"could not notify: {err}")

    def close_after_merge(self, number: int) -> None:
        """After a merged PR and a complete drain, on an autonomous checkout: close the issue's
        helpers as soon as each is proven done and untouched (#33), the issue session only when both
        agents are provably done and idle, then this relay's own session. Mail keeps flowing while it
        waits; a stop request, unread mail to the planner (a human's above all) or autonomy turned
        off stops it; never on a timeout
        alone (after it, only unread implementer mail is overridden, #44). In queue mode a refusal
        is handed to the conductor's backstop (#44)."""
        import agw
        close = self.closer()
        if not close.autonomous():
            self.log("autonomy is off for this checkout; the sessions stay open")
            self.finish_close()
            return
        close.log(f"PR #{number} merged; autonomous close starting")
        if self.state.pop('close_handoff', None) is not None and not self.dry_run:
            # A restarted relay owns the close again; the conductor defers to it while it lives.
            self._save()
        left_open: list[str] = []
        while True:
            if self.stop_file.exists():
                # The launcher is restarting this relay; the pending close resumes after it.
                close.log("NOT closing: stop requested; the close resumes when the relay restarts")
                return
            # The planner's "loop complete" mail (and any human mail) must still be rung.
            self.flush_outbox()
            self.deliver_mail()
            try:
                # Helpers first, and regardless of the agents (#33).
                left_open = close.step_helpers()
            except (agw.CtlError, OSError) as err:
                close.log(f"helper check failed: {err}")
            reasons = close.agent_blockers(number)
            if not reasons or close.overdue_ok():
                break
            if close.timed_out():
                close.log(f"NOT closing, still waiting after {closer.CLOSE_WAIT:.0f}s: " + '; '.join(reasons))
                self.close_alert('; '.join(reasons))
                try:
                    handed = self.hand_off_close(close, number, reasons)
                except (agw.CtlError, OSError) as err:
                    close.log(f"could not hand the close to the queue conductor: {err}")
                    handed = False
                if not handed:
                    self.finish_close()
                return
            pause(self.mail_interval)
        if not close.autonomous():
            close.log("NOT closing: autonomy was turned off during the wait")
            self.finish_close()
            return
        if self.dry_run:
            close.log('[dry-run] would close the issue session and the relay')
            return
        try:
            close.close_issue_session()
            # Before this relay's own session goes (that ends this process), and detached from it (#41).
            close.start_cleanup(number)
            self.close_own_session(close, number, left_open)
        except (agw.CtlError, OSError) as err:
            close.log(f"close failed: {err}")
            self.close_alert(f"a close step failed: {err}")
            self.finish_close()

    def close_own_session(self, close: "closer.Closer", number: int, left_open: list[str]) -> None:
        import agw
        summary = (f"PR #{number} merged; sessions closed"
                   + (f"; left open: {', '.join(left_open)}" if left_open else '')
                   + "; log: .workbench/state/relay-close.log")
        mine = agw.my_pane()
        own = self.own_session(close)
        try:
            agw.notify(mine or 'active', summary, title='workbench relay')
        except (agw.CtlError, OSError) as err:
            self.log(f"could not notify: {err}")
        self.finish_close()
        if own is None:
            close.log(f"leaving this relay's session open: it is not a single-pane '#{close.issue} relay' "
                      f"in workspace {close.workspace_name}")
            return
        close.log(f"closing the relay session {own[1].get('id')}: {summary}")
        agw.clear_restore(mine)
        agw.close_session(own[1].get('id'))

    def own_session(self, close: "closer.Closer") -> tuple[str, dict] | None:
        """(my pane, my session) only when the session is provably the relay's: the name, this repo's
        workspace, and no pane but this one - a human's shell split beside it must never go with it."""
        import agw
        mine = agw.my_pane()
        found = agw.find_pane(mine, agw.tree()) if mine else None
        own = found[1] if found else None
        if (own is None or (found[0].get('name') or '').casefold() != close.workspace_name.casefold()
                or own.get('name') != f'#{close.issue} relay' or agw.panes_of(own) != [mine]):
            return None
        return mine, own

    def deliver_mail(self) -> None:
        import agw
        import peerchat
        announced = set(self.state.get("announced", []))
        for peer in self.peers:
            messages = []
            for path in self.hub.unread(peer.box):
                try:
                    messages.append((path, self.hub.parse_message(path)))
                except FileNotFoundError:
                    # Reading mail moves it out of unread; an agent may do that after our glob.
                    continue
            unread_ids = {message.get('id', path.stem) for path, message in messages}
            for box, mid in list(self.holds):
                if box == peer.box and (self.holds[(box, mid)].clear_pending or
                                        mid not in unread_ids or mid in announced):
                    self.clear(peer, mid)
            for path, message in messages:
                mid = message.get('id', path.stem)
                if mid in announced or (self.holds.get((peer.box, mid)) and
                                       self.holds[(peer.box, mid)].clear_pending):
                    continue
                episode = self.state.get('limits', {}).get(peer.box)
                if (episode and episode.get('kind') == 'limited' and episode.get('announced')
                        and not self.draining):
                    self.hold(peer, mid, 'usage limit')
                    continue
                try:
                    if peer.tool == "claude" and is_busy(agw.pane_text(peer.pane)):
                        raise peerchat.Refused('mid-turn; waiting for the agent to finish')
                    text = peerchat.compose_text("Chat from Workbench: ",
                                                 pointer_text(message, self.agmsg, self.hub_dir))
                    if self.dry_run:
                        self.log(f"[dry-run] would ring {peer.box}: {text}")
                        continue
                    outcome = peerchat.send(peer.pane, peerchat.PROFILES[peer.tool], text,
                                            dry_run=False, retry=False)
                    held = self.clear(peer, mid)
                    duration = f" after holding {now() - held.first_at:.0f}s" if held else ''
                    self.log(f"rang {peer.box} for {mid} ({message.get('subject', '')}) [{outcome}]{duration}")
                except peerchat.AmbiguousComposer as refusal:
                    reason = str(refusal)
                    if peer.tool == 'claude':
                        # #33: most often a greyed prompt suggestion. Agents the workbench launches
                        # have them off; an adopted Claude needs it in the human's own settings.
                        reason += ('; if it is a greyed prompt suggestion, set "promptSuggestionEnabled": false'
                                   ' in ~/.claude/settings.json for this Claude')
                    self.hold(peer, mid, reason, ambiguous_text=refusal.content)
                    continue
                except peerchat.Refused as refusal:
                    # nothing was typed; try again next tick
                    self.hold(peer, mid, str(refusal))
                    continue
                except peerchat.Failed as failure:
                    # Next tick may retry only through peerchat's empty-composer precheck.
                    self.hold(peer, mid, str(failure), failed=True)
                    continue
                except (agw.CtlError, OSError) as err:
                    self.hold(peer, mid, f"terminal not reachable: {err}")
                    continue
                announced.add(mid)
                self.state["announced"] = sorted(announced)
                self._save()

    # pull request ---------------------------------------------------------------------------
    def ignore_finished(self, number: int, state: str) -> None:
        if number in self.state.get('ignored_prs', []) or number in self.state.get('completed_prs', []):
            return
        self.log(f"ignoring finished PR #{number} ({state.lower()}; predates this relay's watch boundary)")
        if not self.dry_run:
            self.state['ignored_prs'] = sorted({*self.state.get('ignored_prs', []), number})
            self._save()

    def retire(self, number: int) -> None:
        if (self.state.get('pr') or {}).get('state') == 'MERGED':
            # Persisted, so a relay that dies or restarts during the close wait resumes it (#27).
            self.state['close_pending'] = number
            # The merge time: mail created after it is nothing the PR needed (#44).
            self.state['close_merged_at'] = self.state['pr'].get('mergedAt')
        self.state['completed_prs'] = sorted({*self.state.get('completed_prs', []), number})
        self.state.pop('pr', None)
        self.state.pop('terminal_mail', None)
        self.state.pop('limits', None)      # the loop is over; nobody fails over any more
        self.log(f'retired finished PR #{number}; a restart can watch the next PR')
        if not self.dry_run:
            self._save()

    def watch_boundary(self) -> datetime | None:
        if 'watch_since' in self.state:
            boundary = timestamp(self.state['watch_since'])
            if boundary is None:
                self.log('invalid saved watch_since; repair relay state before PR watching can resume')
            return boundary
        if self.dry_run and self._dry_watch_since is not None:
            return self._dry_watch_since
        boundary = github_time()
        if boundary is None:
            self.log('GitHub Date unavailable; PR watching not initialized; will retry')
            return None
        if self.dry_run:
            self._dry_watch_since = boundary
        else:
            self.state['watch_since'] = boundary.isoformat(timespec='seconds')
            self._save()  # The boundary must survive a restart before any PR polling.
        return boundary

    def view_pr(self, number: int) -> dict[str, Any] | None:
        snapshot = gh_json(['pr', 'view', str(number), '--repo', self.repo, '--json', PR_FIELDS])
        if snapshot is None:
            return None
        if (snapshot.get('number') != number or snapshot.get('isCrossRepository') is not False
                or snapshot.get('headRefName') != self.branch):
            self.log(f'ignoring PR #{number}: view does not match the requested number, head repository or branch')
            return None
        inline = gh_pages(f'repos/{self.repo}/pulls/{number}/comments')
        if inline is None or snapshot.get('state') not in ('OPEN', 'MERGED', 'CLOSED'):
            return None
        snapshot['inline'] = inline
        return snapshot

    def fetch_pr(self) -> PrFetch | None:
        boundary = self.watch_boundary()
        if boundary is None:
            return None

        def endpoint(state: str) -> str:
            query = urlencode({'state': state, 'head': f'{self.repo.split("/")[0]}:{self.branch}',
                               'per_page': 100})
            return f'repos/{self.repo}/pulls?{query}'

        candidates = gh_pages(endpoint('open'))
        if candidates is None:
            return None
        selected = select_open(candidates, self.repo)
        tracked = self.state.get('pr') or {}
        older = []
        if selected is not None:
            number = selected['number']
        elif (tracked.get('number') in self.state.get('seen_open', [])
              and tracked.get('number') not in self.state.get('completed_prs', [])):
            number = tracked['number']
        else:
            eligible = []
            for pr in gh_pages(endpoint('all')) or []:
                if (pr.get('state') != 'closed' or not same_repo(pr, self.repo)
                        or (pr.get('head') or {}).get('ref') != self.branch
                        or pr['number'] in self.state.get('ignored_prs', [])
                        or pr['number'] in self.state.get('completed_prs', [])):
                    continue
                created = timestamp(pr.get('created_at'))
                if pr['number'] in self.state.get('seen_open', []):
                    eligible.append((created or boundary, pr['number']))
                elif created is None:
                    self.log(f'PR #{pr["number"]}: creation time unavailable; will retry')
                elif created >= boundary:
                    eligible.append((created, pr['number']))
                else:
                    older.append(pr)
            if not eligible:
                for pr in older:
                    self.ignore_finished(pr['number'], 'MERGED' if pr.get('merged_at') else 'CLOSED')
                return None
            _, number = max(eligible)
        snapshot = self.view_pr(number)
        if snapshot is None:
            return None
        fast = False
        if snapshot.get('state') in ('MERGED', 'CLOSED'):
            if number in self.state.get('completed_prs', []):
                return None
            if number not in self.state.get('seen_open', []):
                created = timestamp(snapshot.get('createdAt'))
                if created is None:
                    self.log(f'PR #{number}: creation time unavailable; will retry')
                    return None
                if created < boundary:
                    self.ignore_finished(number, snapshot['state'])
                    return None
            # A legacy seen_open entry may have lost its snapshot during a PR switch.
            # It needs explicit terminal events too, regardless of its creation time.
            fast = tracked.get('number') != number
        previous = None
        if tracked and tracked['number'] != number:
            previous = self.view_pr(tracked['number'])
            if previous is None:
                return None  # Resolve the old watch before committing the switch.
        # Timeline timestamps identify a reopening even after the relay's state is lost.
        # Only discovery/reopening needs this extra request; retain the identity across polls.
        if fast or (snapshot['state'] == 'OPEN' and
                    (tracked.get('number') != number or tracked.get('state') != 'OPEN')):
            timeline = gh_pages(f'repos/{self.repo}/issues/{number}/timeline')
            if timeline is None:
                return None
            reopened = [timestamp(event.get('created_at')) for event in timeline
                        if event.get('event') == 'reopened']
            if any(instant is None for instant in reopened):
                self.log(f'PR #{number}: reopening time unavailable; will retry')
                return None
            snapshot['openingAt'] = (max(reopened).isoformat() if reopened else snapshot.get('createdAt'))
        elif tracked.get('number') == number and 'openingAt' in tracked:
            snapshot['openingAt'] = tracked['openingAt']
        for pr in older:
            self.ignore_finished(pr['number'], 'MERGED' if pr.get('merged_at') else 'CLOSED')
        return PrFetch(snapshot, fast, previous)

    def flush_outbox(self) -> bool:
        if self.dry_run or not self.state.get('outbox'):
            return True
        outbox = self.state['outbox']
        try:
            for message in outbox:
                self.hub.write_message(**message)
            self.state.pop('outbox')
            self._save()
        except OSError as err:
            self.state['outbox'] = outbox
            self.log(f'outbox publication failed; will retry on a later tick: {err}')
            return False
        return True

    def watch_pr(self) -> bool:
        """Returns True when the PR is terminal; its filed notices must still be delivered."""
        if not self.flush_outbox():
            return False
        result = self.fetch_pr()
        if result is None:
            return False
        snapshot, fast = result.snapshot, result.fast
        seen_open = set(self.state.get('seen_open', []))
        completed = set(self.state.get('completed_prs', []))
        previous = result.previous
        if (previous and previous['number'] in seen_open
                and previous['state'] in ('MERGED', 'CLOSED')):
            snapshot, fast = previous, False
            previous = None  # Drain this watch; the successor belongs to the next run.
        if fast:
            if snapshot['number'] in seen_open:
                self.log(f'PR #{snapshot["number"]}: recovering terminal events for a previous watch')
            else:
                self.log(f'PR #{snapshot["number"]} opened and finished between polls')
        terminal = snapshot['state'] in ('MERGED', 'CLOSED')
        terminal_mail = list(self.state.get('terminal_mail', []))
        events = fast_terminal_events(snapshot) if fast else pr_events(self.state.get("pr"), snapshot)
        numbered_events = [(snapshot['number'], event) for event in events]
        if previous:
            numbered_events = [(previous['number'], event)
                               for event in pr_events(self.state.get('pr'), previous)] + numbered_events
            completed.add(previous['number'])
            self.log(f'PR #{previous["number"]}: resolved previous watch ({previous["state"]}); '
                     f'switching to PR #{snapshot["number"]}')
        outbox = []
        for number, event in numbered_events:
            recipients = ["claude"]
            terminal_event = event.get('terminal', False)
            if terminal_event:
                recipients = [p.box for p in self.peers]
            for box in recipients:
                if self.dry_run:
                    self.log(f"[dry-run] would file github mail for {box}: {event['subject']}")
                    continue
                mid = event_message_id(number, event, box)
                outbox.append(dict(to=box, sender="github", subject=event["subject"],
                                   body=event["body"], kind=event["kind"], message_id=mid))
                if terminal_event:
                    terminal_mail.append([box, mid])
            if not self.dry_run:
                self.log(f"github: {event['subject']}")
        if not self.dry_run:
            seen_open.add(snapshot['number'])
            if snapshot.get('state') == 'OPEN':
                completed.discard(snapshot['number'])
                if snapshot['number'] in self.state.get('ignored_prs', []):
                    self.state['ignored_prs'] = [number for number in self.state['ignored_prs']
                                                 if number != snapshot['number']]
            self.state['seen_open'] = sorted(seen_open)
            self.state["pr"] = snapshot
            self.state['terminal_mail'] = terminal_mail
            if completed or 'completed_prs' in self.state:
                self.state['completed_prs'] = sorted(completed)
            self.state.pop('event_sequence', None)  # Migrate the obsolete counter.
            if outbox:
                self.state['outbox'] = outbox
            self._save()
            self.flush_outbox()
        return terminal

    def pending_terminal_mail(self) -> set[tuple[str, str]]:
        announced = set(self.state.get('announced', []))
        targets = {tuple(target) for target in self.state.get('terminal_mail', [])}
        unread = {(box, path.stem)
                  for box in {box for box, _ in targets} for path in self.hub.unread(box)}
        return {(box, mid) for box, mid in targets & unread if mid not in announced}

    def run(self) -> int:
        self.log(f"relay up: {self.repo} {self.branch}; mailbox {self.hub_dir}")
        next_pr = 0.0
        saved_terminal = (self.state.get('pr') or {}).get('state') in ('MERGED', 'CLOSED')
        # Replayed mail might already be read. Still finish this saved watch without polling
        # GitHub again; the drain will immediately retire it if no delivery or reset remains.
        drain_deadline = now() + TERMINAL_DRAIN_TIMEOUT if saved_terminal else None
        self.draining = drain_deadline is not None
        if drain_deadline is not None:
            if self.dry_run:
                self.log('[dry-run] saved PR is finished; no final mail filed')
                return 0
            self.log('saved PR is finished; resuming final notice drain')
        if self.state.get('close_pending') and not self.dry_run:
            self.close_after_merge(self.state['close_pending'])
        next_limit = 0.0
        while True:
            if self.stop_file.exists():
                self.log("stop file found; exiting")
                return 0
            published = self.flush_outbox()
            if drain_deadline is None and now() >= next_limit:
                # After a merge or close only the final notices matter; nobody fails over then.
                next_limit = now() + self.limit_interval
                self.check_limits()
            self.deliver_mail()
            if drain_deadline is not None:
                pending = self.pending_terminal_mail()
                resets = {key for key, held in self.holds.items() if held.clear_pending}
                resets.update((box, '') for box in self.state.get('reset_pending', []))
                if not pending and not resets and published:
                    number, state = self.state['pr']['number'], self.state['pr'].get('state')
                    self.retire(number)
                    self.log("PR is finished; final notices delivered or read; the relay's job is done")
                    if state == 'MERGED':
                        self.close_after_merge(number)
                    return 0
                if now() >= drain_deadline:
                    details = []
                    pending.update((m['to'], m['message_id']) for m in self.state.get('outbox', []))
                    for box, mid in sorted(pending | resets):
                        held = self.holds.get((box, mid))
                        reason = (held.reason if held else 'status reset pending' if (box, mid) in resets
                                  else 'not delivered')
                        details.append(f'{box}/{mid}: {reason}')
                    detail = '; '.join(details)
                    self.log(f"PR is finished; drain deadline reached; still held or awaiting status reset: {detail}")
                    return 0
            elif published and now() >= next_pr:
                next_pr = now() + self.pr_interval
                if self.watch_pr():
                    if self.dry_run:
                        self.log('[dry-run] PR is finished; no final mail filed')
                        return 0
                    drain_deadline = now() + TERMINAL_DRAIN_TIMEOUT
                    self.draining = True
                    self.log('PR is finished; draining final notices before exit')
                    if not self.state.get('outbox'):
                        continue
            delay = self.mail_interval
            if drain_deadline is not None:
                delay = min(delay, max(0, drain_deadline - now()))
            pause(delay)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="agworkbench relay: mail doorbell + PR watcher")
    parser.add_argument("--hub", required=True, help="the workbench mailbox directory (.workbench)")
    parser.add_argument("--claude-pane", required=True)
    parser.add_argument("--codex-pane", required=True, help="the implementer's pane (mailbox box 'codex')")
    parser.add_argument("--implementer-tool", choices=("codex", "claude"), default="codex",
                        help="which agent runs the implementer pane; picks the peerchat profile it is rung with")
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--branch", required=True)
    parser.add_argument("--mail-interval", type=float, default=5.0)
    parser.add_argument("--pr-interval", type=float, default=60.0)
    parser.add_argument("--limit-interval", type=float, default=30.0,
                        help="seconds between usage-limit checks of each pane (#24)")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def peers_for(args: argparse.Namespace) -> list[Peer]:
    """The implementer keeps the box 'codex' whichever agent runs it; its tool picks the profile."""
    check_panes(args.claude_pane, args.codex_pane)
    return [Peer("claude", "claude", args.claude_pane), Peer("codex", args.implementer_tool, args.codex_pane)]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    peers = peers_for(args)
    relay = Relay(Path(args.hub), peers, args.repo, args.branch, args.mail_interval,
                  args.pr_interval, dry_run=args.dry_run, limit_interval=args.limit_interval)
    try:
        return relay.run()
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
