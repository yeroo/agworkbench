#!/usr/bin/env python3
"""relay - the workbench's doorbell and its eye on GitHub.

Two jobs, one loop, one process per issue, running in its own visible agwinterm session:

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

Nothing here polls on behalf of an agent: agents are woken by the relay and otherwise idle. The
relay itself polls the mailbox directory and the GitHub API, which cannot push to it.

Typing into a pane goes through `peerchat` (vendored, fail-closed): a composer that is not
provably empty, a dialog on screen, or a pane that is not an agent is a refusal, never a send. A
refusal before typing is retried on the next tick. Submit keys are verified and retried by
peerchat; a failed ring is announced only after a later send succeeds from an empty composer.
"""

from __future__ import annotations

import argparse
import json
import os
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

PR_FIELDS = "number,url,state,createdAt,reviewDecision,mergedAt,reviews,comments,headRefName,isCrossRepository"
HOLD_ALERT_AFTER = 60.0
AMBIGUOUS_ALERT_AFTER = 600.0
ALERT_EVERY = 300.0
TERMINAL_DRAIN_TIMEOUT = 30 * 60.0


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
    if old is None:
        if new.get('state') != 'OPEN':
            return events
        events.append({"kind": "note", "subject": f"PR #{number} is open",
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
                       "body": body})

    seen_comments = {c.get("id") for c in old.get("comments") or []}
    for comment in new.get("comments") or []:
        if comment.get("id") in seen_comments:
            continue
        author = (comment.get("author") or {}).get("login", "?")
        events.append({"kind": "message", "subject": f"PR #{number}: comment from {author}",
                       "body": (comment.get("body") or "").strip()})

    seen_inline = {c.get("id") for c in old.get("inline") or []}
    for comment in new.get("inline") or []:
        if comment.get("id") in seen_inline:
            continue
        author = (comment.get("user") or {}).get("login", "?")
        where = f"{comment.get('path')}:{comment.get('line') or comment.get('original_line') or '?'}"
        events.append({"kind": "review", "subject": f"PR #{number}: line comment from {author} on {where}",
                       "body": (comment.get("body") or "").strip()})

    if new.get("reviewDecision") and new.get("reviewDecision") != old.get("reviewDecision"):
        events.append({"kind": "note", "subject": f"PR #{number}: review decision is now "
                                                   f"{new['reviewDecision']}", "body": new.get("url", "")})

    state = new.get("state")
    if state != old.get("state"):
        if state == "MERGED":
            events.append({"kind": "note", "subject": f"PR #{number} MERGED - the loop is complete",
                           "body": f"Merged at {new.get('mergedAt')}. {new.get('url')}", "terminal": True})
        elif state == "CLOSED":
            events.append({"kind": "note", "subject": f"PR #{number} was CLOSED without merging",
                           "body": new.get("url", ""), "terminal": True})
    return events


def fast_terminal_events(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Discover a PR and its existing discussion, then announce its terminal transition."""
    opened = dict(snapshot, state='OPEN')
    return pr_events(None, opened) + pr_events(opened, snapshot)


def timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


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


def finished(snapshot: dict[str, Any] | None, seen_open: set[int], completed_prs: set[int]) -> bool:
    return (bool(snapshot) and snapshot.get('state') in ('MERGED', 'CLOSED')
            and snapshot.get('number') in seen_open and snapshot.get('number') not in completed_prs)


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

class Relay:
    def __init__(self, hub_dir: Path, peers: list[Peer], repo: str, branch: str,
                 mail_interval: float, pr_interval: float, dry_run: bool = False):
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
        self.dry_run = dry_run
        self.state_file = hub_dir / "state" / "relay.json"
        self.stop_file = hub_dir / "state" / "relay.stop"
        self.state = self._load()
        saved_pr = self.state.get('pr')
        saved_branch = self.state.get('branch', (saved_pr or {}).get('headRefName'))
        changed = self.state.get('branch') != self.branch
        if saved_branch != self.branch or (saved_pr and saved_pr.get('headRefName') != self.branch):
            branch_keys = ('pr', 'terminal_mail', 'ignored_prs', 'seen_open', 'completed_prs', 'watch_since')
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
                and not self.pending_terminal_mail() and not self.state.get('reset_pending')):
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
                    self.hold(peer, mid, str(refusal), ambiguous_text=refusal.content)
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
        self.state['completed_prs'] = sorted({*self.state.get('completed_prs', []), number})
        self.state.pop('pr', None)
        self.state.pop('terminal_mail', None)
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

    def fetch_pr(self) -> dict[str, Any] | None:
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
                if created is None:
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
        snapshot = gh_json(['pr', 'view', str(number), '--repo', self.repo, '--json', PR_FIELDS])
        if snapshot is None:
            return None
        if (snapshot.get('number') != number or snapshot.get('isCrossRepository') is not False
                or snapshot.get('headRefName') != self.branch):
            self.log(f'ignoring PR #{number}: head repository or branch does not match')
            return None
        inline = gh_pages(f'repos/{self.repo}/pulls/{number}/comments')
        if inline is None:
            return None
        snapshot['inline'] = inline
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
                # Only a validated view with a successful inline fetch can establish
                # observation. watch_pr commits it with the actual terminal snapshot.
                snapshot['_observed_after_watch'] = True
        elif snapshot.get('state') != 'OPEN':
            return None
        for pr in older:
            self.ignore_finished(pr['number'], 'MERGED' if pr.get('merged_at') else 'CLOSED')
        return snapshot

    def watch_pr(self) -> bool:
        """Returns True when the PR is terminal; its filed notices must still be delivered."""
        snapshot = self.fetch_pr()
        if snapshot is None:
            return False
        seen_open = set(self.state.get('seen_open', []))
        completed = set(self.state.get('completed_prs', []))
        fast = snapshot.pop('_observed_after_watch', False) and snapshot['number'] not in seen_open
        if fast:
            seen_open.add(snapshot['number'])
            self.log(f'PR #{snapshot["number"]} opened and finished between polls')
        terminal = finished(snapshot, seen_open, completed)
        if snapshot.get('state') != 'OPEN' and not terminal:
            self.ignore_finished(snapshot['number'], snapshot.get('state', 'finished'))
            return False
        terminal_mail = list(self.state.get('terminal_mail', []))
        events = fast_terminal_events(snapshot) if fast else pr_events(self.state.get("pr"), snapshot)
        for event in events:
            recipients = ["claude"]
            terminal_event = event.get('terminal', False)
            if terminal_event:
                recipients = [p.box for p in self.peers]
            for box in recipients:
                if self.dry_run:
                    self.log(f"[dry-run] would file github mail for {box}: {event['subject']}")
                    continue
                path = self.hub.write_message(to=box, sender="github", subject=event["subject"],
                                              body=event["body"], kind=event["kind"])
                if terminal_event:
                    terminal_mail.append([box, path.stem])
            if not self.dry_run:
                self.log(f"github: {event['subject']}")
        if not self.dry_run:
            if snapshot.get('state') == 'OPEN':
                seen_open.add(snapshot['number'])
                for key in ('completed_prs', 'ignored_prs'):
                    if snapshot['number'] in self.state.get(key, []):
                        self.state[key] = [number for number in self.state[key] if number != snapshot['number']]
            self.state['seen_open'] = sorted(seen_open)
            self.state["pr"] = snapshot
            self.state['terminal_mail'] = terminal_mail
            self._save()
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
        drain_deadline = (now() + TERMINAL_DRAIN_TIMEOUT if saved_terminal and
                          (self.pending_terminal_mail() or self.state.get('reset_pending')) else None)
        if drain_deadline is not None:
            if self.dry_run:
                self.log('[dry-run] saved PR is finished; no final mail filed')
                return 0
            self.log('saved PR is finished; resuming final notice drain')
        while True:
            if self.stop_file.exists():
                self.log("stop file found; exiting")
                return 0
            self.deliver_mail()
            if drain_deadline is not None:
                pending = self.pending_terminal_mail()
                resets = {key for key, held in self.holds.items() if held.clear_pending}
                resets.update((box, '') for box in self.state.get('reset_pending', []))
                if not pending and not resets:
                    self.retire(self.state['pr']['number'])
                    self.log("PR is finished; final notices delivered or read; the relay's job is done")
                    return 0
                if now() >= drain_deadline:
                    details = []
                    for box, mid in sorted(pending | resets):
                        held = self.holds.get((box, mid))
                        reason = (held.reason if held else 'status reset pending' if (box, mid) in resets
                                  else 'not delivered')
                        details.append(f'{box}/{mid}: {reason}')
                    detail = '; '.join(details)
                    self.log(f"PR is finished; drain deadline reached; still held or awaiting status reset: {detail}")
                    return 0
            elif now() >= next_pr:
                next_pr = now() + self.pr_interval
                if self.watch_pr():
                    if self.dry_run:
                        self.log('[dry-run] PR is finished; no final mail filed')
                        return 0
                    drain_deadline = now() + TERMINAL_DRAIN_TIMEOUT
                    self.log('PR is finished; draining final notices before exit')
                    continue
            delay = self.mail_interval
            if drain_deadline is not None:
                delay = min(delay, max(0, drain_deadline - now()))
            pause(delay)


def main() -> int:
    parser = argparse.ArgumentParser(description="agworkbench relay: mail doorbell + PR watcher")
    parser.add_argument("--hub", required=True, help="the workbench mailbox directory (.workbench)")
    parser.add_argument("--claude-pane", required=True)
    parser.add_argument("--codex-pane", required=True)
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--branch", required=True)
    parser.add_argument("--mail-interval", type=float, default=5.0)
    parser.add_argument("--pr-interval", type=float, default=60.0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    check_panes(args.claude_pane, args.codex_pane)
    peers = [Peer("claude", "claude", args.claude_pane), Peer("codex", "codex", args.codex_pane)]
    relay = Relay(Path(args.hub), peers, args.repo, args.branch, args.mail_interval,
                  args.pr_interval, dry_run=args.dry_run)
    try:
        return relay.run()
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
