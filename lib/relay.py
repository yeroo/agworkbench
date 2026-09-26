#!/usr/bin/env python3
"""relay - the workbench's doorbell and its eye on GitHub.

Five jobs, one loop, one process per issue, running in its own visible agwinterm session:

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
   a stop request, a human's mail or autonomy turned off stops it, and it never closes on a timeout.
   A pending close survives a restart (`close_pending`).

5. **Stalls (#45).** On the same reads it watches for a loop that sits idle with nothing to wake it:
   both agent panes provably idle, no unread mail, no running helper, and the loop not done, not
   waiting on the human (`state/waiting.json`, loop.json `blocked`/`pr-open`, a PR open for review).
   After `stallMinutes` (config, default 15) it mails the planner one `stall` pointer; after two more
   periods with no progress it reports the loop blocked (blocked status and sound, waiting.json, and
   loop.json in queue mode). Progress - a commit, mail, a helper, a loop report - resets it. It
   never types anything but the mail pointer, and never answers a prompt.

Nothing here polls on behalf of an agent: agents are woken by the relay and otherwise idle. The
relay itself polls the mailbox directory, the GitHub API, the two agent panes (for limits and
stalls), and for stalls also the terminal's session tree and `git rev-parse HEAD`, none of which
can push to it.

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

PR_FIELDS = ("number,url,state,createdAt,updatedAt,closedAt,reviewDecision,mergedAt,reviews,comments,headRefName,"
             "isCrossRepository,statusCheckRollup")
HOLD_ALERT_AFTER = 60.0
AMBIGUOUS_ALERT_AFTER = 600.0
ALERT_EVERY = 300.0
TERMINAL_DRAIN_TIMEOUT = 30 * 60.0
LIMIT_READS = 2          # consecutive reads that start (or end) a usage-limit episode
STALL_MINUTES = 15.0     # the default stall period (#45); `stallMinutes` in ~/.agworkbench.json, 0 = off
LAST_WORDS_MAX = 300     # how much of the implementer's last line a stall pointer quotes


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


def stall_setting() -> float:
    """`stallMinutes` from ~/.agworkbench.json (AGWORKBENCH_CONFIG honoured): a number >= 0, 0 = off.
    The launcher refuses an invalid value; one that slips through here reads as the default."""
    path = Path(os.environ.get("AGWORKBENCH_CONFIG") or (Path.home() / ".agworkbench.json"))
    try:
        config = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return STALL_MINUTES
    value = config.get("stallMinutes") if isinstance(config, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return STALL_MINUTES
    return float(value)


def ci_pending(pr: dict[str, Any]) -> list[str]:
    """The PR's checks that are still running, from the snapshot's statusCheckRollup (#45 r1): a
    check run not COMPLETED, a commit status PENDING or EXPECTED."""
    pending = []
    for item in pr.get("statusCheckRollup") or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("__typename") or ("CheckRun" if "status" in item else "StatusContext")
        if kind == "CheckRun" and item.get("status") != "COMPLETED":
            pending.append(str(item.get("name") or "?"))
        elif kind == "StatusContext" and item.get("state") in ("PENDING", "EXPECTED"):
            pending.append(str(item.get("context") or "?"))
    return pending


def git_head(root: Path) -> str | None:
    try:
        done = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True,
                              timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def last_words(text: str | None, tool: str) -> str | None:
    """What an agent last said (#45): the last paragraph above its composer box - not its footer or
    status line, which are the pane's real last rows. None when the composer cannot be found."""
    import peerchat
    lines = (text or "").splitlines()
    prompt_re = peerchat.CLAUDE_PROMPT_RE if tool == "claude" else peerchat.CODEX_PROMPT_RE
    prompt = next((i for i in range(len(lines) - 1, -1, -1) if prompt_re.match(lines[i])), None)
    if prompt is None:
        return None
    rule = next((i for i in range(prompt - 1, -1, -1) if peerchat.RULE_RE.match(lines[i])), None)
    if rule is None:
        return None
    above = lines[:rule]
    # Claude's turn timer ("✻ Brewed for 1m 0s") sits between the answer and the box.
    while above and (not above[-1].strip() or above[-1].lstrip().startswith("✻")):
        above.pop()
    start = len(above)
    while start and above[start - 1].strip():
        start -= 1
    words = " ".join(row.strip() for row in above[start:])
    if not words:
        return None
    return words if len(words) <= LAST_WORDS_MAX else "…" + words[-LAST_WORDS_MAX:]


class StallWatch:
    """#45: a loop that sits idle with nothing to wake it - its planner's mail waiter killed under
    memory pressure, a helper's result nobody watches, an implementer waiting on a question to the human.

    One `tick` per limit interval, with the pane texts the limit check read. Level 0: stalled for S
    (both panes idle, nothing exempts it) -> one `stall` mail to the planner, level 1. Level 1: 2S
    after the pointer, with no progress and both panes idle for at least S -> report the loop
    blocked, level 2. Level 2: nothing more. Progress (the fingerprint changes) or an exemption
    returns to level 0 with a fresh clock. Session status is never read: agwinterm's agent hooks
    rewrite it every turn. State is in memory, so a restart only makes a stall later, never earlier."""

    def __init__(self, relay: "Relay", minutes: float):
        self.relay = relay
        self.period = max(0.0, float(minutes or 0)) * 60.0
        self.level = 0
        self.since: float | None = None          # stalled (idle, not exempt) since
        self.idle_since: float | None = None     # both panes idle since
        self.pointer_at: float | None = None
        self.fingerprint: tuple | None = None
        self.quiet: dict[str, tuple[str, float]] = {}   # marker-less helper pane -> (tail hash, unchanged since)
        self.sent: set[str] = set()              # this relay's stall mail: neither progress nor unread
        self.last_note: str | None = None

    @property
    def state_dir(self) -> Path:
        return self.relay.hub_dir / "state"

    def note(self, text: str) -> None:
        """Log a change of mind once, not every tick."""
        if text != self.last_note:
            self.last_note = text
            self.relay.log(f"stall watch: {text}")

    def read_json(self, name: str) -> Any:
        try:
            return json.loads((self.state_dir / name).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return None

    # --- what the loop is doing ---------------------------------------------------------------
    def helpers(self, snapshot) -> tuple[list[dict], list[str], list[str]]:
        """(helper sessions, the live ones' names, notes on quiet ones). A helper without a completion
        marker is live while its pane changed within 2S - one killed under memory pressure leaves its
        pane on screen and never writes the marker. The human's revdiff is always live."""
        import agw
        close = closer.Closer(self.relay.hub_dir, self.relay.repo, closer.issue_from_branch(self.relay.branch),
                              self.relay.peers, log=lambda text: None, clock=now, dry_run=True)
        sessions = close.helper_sessions(snapshot)
        live, quiet, seen = [], [], set()
        for session in sessions:
            name = session.get("name") or "?"
            panes = agw.panes_of(session)
            if panes and all(close.marker_path(pane).exists() for pane in panes):
                continue
            if name.endswith(" your review") or not panes:
                live.append(name)
                continue
            pane = panes[0]
            seen.add(pane)
            try:
                tail = limits.tail_hash(agw.pane_text(pane))
            except (agw.CtlError, OSError):
                live.append(name)       # unreadable: never call a helper dead without evidence
                continue
            previous = self.quiet.get(pane)
            if previous is None or previous[0] != tail:
                self.quiet[pane] = (tail, now())
                live.append(name)
            elif now() - previous[1] < 2 * self.period:
                live.append(name)
            else:
                quiet.append(f"helper {name} has no completion marker and its pane has not changed for "
                             f"{(now() - previous[1]) / 60:.0f} min")
        for pane in set(self.quiet) - seen:
            self.quiet.pop(pane)
        return sessions, live, quiet

    def unread(self) -> list[str]:
        found = []
        for box in ("claude", "codex"):
            for path in self.relay.hub.unread(box):
                if path.stem in self.sent:
                    continue
                try:
                    message = self.relay.hub.parse_message(path)
                except (OSError, ValueError):
                    message = {}
                if message.get("from") == "relay" and message.get("kind") == "stall":
                    continue
                found.append(f"{box}/{path.stem}")
        return found

    def exemptions(self, live: list[str]) -> list[str]:
        """Why this loop is not stalled although it may look idle. Empty when nothing exempts it."""
        reasons = []
        if (self.state_dir / "loop-done.json").exists():
            reasons.append("the loop is done")
        if (self.state_dir / "waiting.json").exists():
            reasons.append("waiting on the human (state/waiting.json)")
        loop = self.read_json("loop.json")
        if isinstance(loop, dict) and loop.get("state") in ("blocked", "pr-open"):
            reasons.append(f"loop.json says {loop.get('state')}")
        pr = self.relay.state.get("pr") or {}
        settings = self.read_json("implementer.json")
        if pr.get("state") == "OPEN":
            if not (isinstance(settings, dict) and settings.get("autoMerge") is True):
                reasons.append(f"PR #{pr.get('number')} is open for review")
            elif ci_pending(pr):
                # Under auto-merge the planner waits on CI with a background wait-ci the relay cannot see.
                reasons.append(f"PR #{pr.get('number')} CI running: {', '.join(ci_pending(pr))}")
        unread = self.unread()
        if unread:
            reasons.append(f"unread mail {', '.join(unread)}")
        if live:
            reasons.append(f"helper running: {', '.join(live)}")
        if self.relay.state.get("limits"):
            reasons.append(f"usage-limit episode: {', '.join(sorted(self.relay.state['limits']))}")
        return reasons

    def current_fingerprint(self, sessions: list[dict]) -> tuple:
        """Everything whose change is progress: a commit, any mail (but this relay's stall mail), a
        helper session or marker, a loop report, the done record, the waiting record."""
        messages = []
        for box in ("claude", "codex"):
            directory = self.relay.hub_dir / "inbox" / box
            for folder in (directory, directory / "read", directory / "archive"):
                messages += [path.stem for path in folder.glob("*.md") if path.stem not in self.sent]
        markers = sorted(path.name for path in (self.state_dir / "helpers").glob("*.done"))
        loop = self.read_json("loop.json")
        return (git_head(self.relay.hub_dir.parent), tuple(sorted(messages)),
                tuple(sorted(str(session.get("id")) for session in sessions)), tuple(markers),
                (loop.get("rev"), loop.get("state")) if isinstance(loop, dict) else None,
                (self.state_dir / "loop-done.json").exists(), (self.state_dir / "waiting.json").exists())

    def busy(self, texts: dict[str, Any]) -> list[str]:
        reasons = []
        for peer in self.relay.peers:
            text = texts.get(peer.box)
            if not isinstance(text, str):
                reasons.append(f"{peer.box} pane unreadable")
            else:
                reasons += closer.idle_blockers(peer, text)
        return reasons

    # --- the tick -------------------------------------------------------------------------------
    def reset(self, why: str) -> None:
        if self.level or self.since is not None:
            self.note(f"clock reset ({why})")
        self.level, self.since, self.pointer_at = 0, None, None

    def tick(self, texts: dict[str, Any]) -> None:
        if self.period <= 0:
            return
        import agw
        instant = now()
        try:
            snapshot = agw.tree()
        except (agw.CtlError, OSError) as err:
            self.note(f"terminal tree unreadable ({err}); not judging")
            return
        sessions, live, quiet = self.helpers(snapshot)
        fingerprint = self.current_fingerprint(sessions)
        if self.fingerprint is not None and fingerprint != self.fingerprint:
            self.reset("progress")
        self.fingerprint = fingerprint
        busy = self.busy(texts)
        if busy:
            self.idle_since = None
        elif self.idle_since is None:
            self.idle_since = instant
        exempt = self.exemptions(live)
        if exempt:
            self.reset(exempt[0])
            self.note(f"not stalled: {'; '.join(exempt)}")
            return
        if self.level == 0:
            if busy:
                self.since = None
                self.note(f"not stalled: {'; '.join(busy)}")
                return
            if self.since is None:
                self.since = instant
                self.note("both panes idle with nothing to wake them; stall clock started")
            if instant - self.since >= self.period and self.pointer(instant - self.since, quiet, texts):
                self.level, self.pointer_at = 1, instant
        elif self.level == 1:
            # A busy pane after the pointer (the planner reading it) does not reset the level, but
            # escalation waits until both panes have been idle for a whole period: never mid-work.
            if (instant - self.pointer_at >= 2 * self.period and self.idle_since is not None
                    and instant - self.idle_since >= self.period):
                self.escalate(instant - self.since, quiet)
                self.level = 2
                # Its own records (waiting.json, loop.json) are not progress; the planner removing them is.
                self.fingerprint = self.current_fingerprint(sessions)

    # --- acting ---------------------------------------------------------------------------------
    def implementer_line(self, texts: dict[str, Any]) -> str | None:
        for peer in self.relay.peers:
            if peer.box == "codex" and isinstance(texts.get(peer.box), str):
                return last_words(texts[peer.box], peer.tool)
        return None

    def pointer(self, idle: float, quiet: list[str], texts: dict[str, Any]) -> bool:
        """File the stall pointer. False when it could not be filed: the level stays, and the next
        tick tries again - an escalation never cites a pointer that does not exist."""
        minutes = f"{idle / 60:.0f}"
        subject = f"stall: loop idle for {minutes} min, nothing unread, no running helper"
        body = [f"The relay has seen this loop idle for {minutes} minutes: both agent panes idle with an empty",
                "composer, no unread mail in either box, no running helper, no PR open for review, and the",
                "loop neither done nor waiting on the human.", ""]
        body += [f"- {line}" for line in quiet] + ([""] if quiet else [])
        words = self.implementer_line(texts)
        if words:
            body += [f"The implementer's last line: {words}", ""]
        body += ["Next step: check your mail waiter - it may have been killed under memory pressure; rearm it",
                 "if it is not running - and any finished helper (mail from `helper`,",
                 "`.workbench/state/helpers/*.done`, `.workbench/review/`). Then continue the loop, or, if it",
                 "waits on the human, say so and run `wb.py status blocked --sound` (queue mode: also",
                 "`wb.py loop-state blocked --reason ...`).", "",
                 f"With no progress for another {2 * self.period / 60:.0f} minutes the relay reports the loop blocked."]
        if self.relay.dry_run:
            self.relay.log(f"[dry-run] would mail the planner: {subject}")
            return True
        try:
            path = self.relay.hub.write_message(to="claude", sender="relay", kind="stall", subject=subject,
                                                body="\n".join(body))
        except OSError as err:
            self.relay.log(f"could not file the stall pointer: {err}")
            return False
        self.sent.add(Path(path).stem)
        self.relay.log(f"STALL pointer to the planner: {subject}")
        return True

    def escalate(self, idle: float, quiet: list[str]) -> None:
        import agw
        summary = (f"stalled: loop idle for {idle / 60:.0f} min, no progress since the relay's stall pointer"
                   + (f"; {quiet[0]}" if quiet else ""))
        if self.relay.dry_run:
            self.relay.log(f"[dry-run] would report the loop blocked: {summary}")
            return
        self.relay.log(f"ALERT {summary}")
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            (self.state_dir / "waiting.json").write_text(
                json.dumps({"at": wall(), "by": "relay", "reason": summary}), encoding="utf-8")
        except OSError as err:
            self.relay.log(f"could not write state/waiting.json: {err}")
        if (self.state_dir / "queue-member.json").exists():
            try:
                import conductor
                identity = conductor.read_json(self.state_dir / "claude.json")
                conductor.write_loop_state(self.relay.hub_dir.parent, "blocked", reason=summary,
                                           loop_id=identity.get("sessionId"))
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as err:
                self.relay.log(f"could not report the stall to the queue: {err}")
        for peer in self.relay.peers:
            if peer.box != "claude":
                continue
            try:
                agw.set_status("blocked", sound=True, blink=True, pane_id=peer.pane)
            except (agw.CtlError, OSError) as err:
                self.relay.log(f"could not set blocked status: {err}")
            try:
                agw.notify(peer.pane, summary, title="workbench relay")
            except (agw.CtlError, OSError) as err:
                self.relay.log(f"could not notify: {err}")


class Relay:
    def __init__(self, hub_dir: Path, peers: list[Peer], repo: str, branch: str,
                 mail_interval: float, pr_interval: float, dry_run: bool = False,
                 limit_interval: float = 30.0, stall_minutes: float = 0.0):
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
        # Off unless given: main() passes the configured value (#45).
        self.stall = StallWatch(self, stall_minutes)
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
    def read_panes(self) -> dict[str, Any]:
        """Each agent pane's text, read once per limit interval for the limit check and the stall
        watch; a read that failed is its exception."""
        import agw
        texts: dict[str, Any] = {}
        for peer in self.peers:
            try:
                texts[peer.box] = agw.pane_text(peer.pane)
            except (agw.CtlError, OSError) as err:
                texts[peer.box] = err
        return texts

    def check_limits(self, texts: dict[str, Any]) -> None:
        """Classify each pane (texts from read_panes); an episode seen on LIMIT_READS consecutive reads
        is announced once."""
        episodes = dict(self.state.get('limits', {}))
        changed = False
        for peer in self.peers:
            text = texts.get(peer.box)
            if not isinstance(text, str):
                self.log(f"limit check: cannot read {peer.box}: {text}")
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
        if self.state.pop('close_pending', None) is not None and not self.dry_run:
            self._save()

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
        waits; a stop request, a human's mail or autonomy turned off stops it; never on a timeout."""
        import agw
        close = self.closer()
        if not close.autonomous():
            self.log("autonomy is off for this checkout; the sessions stay open")
            self.finish_close()
            return
        close.log(f"PR #{number} merged; autonomous close starting")
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
            if not reasons:
                break
            if close.timed_out():
                close.log(f"NOT closing, still waiting after {closer.CLOSE_WAIT:.0f}s: " + '; '.join(reasons))
                self.close_alert('; '.join(reasons))
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
        found = agw.find_pane(mine, agw.tree()) if mine else None
        try:
            agw.notify(mine or 'active', summary, title='workbench relay')
        except (agw.CtlError, OSError) as err:
            self.log(f"could not notify: {err}")
        self.finish_close()
        # Its own session only when it is provably the relay's: the name, this repo's workspace, and
        # no pane but this one - a human's shell split beside it must never go with it.
        own = found[1] if found else None
        if (own is None or (found[0].get('name') or '').casefold() != close.workspace_name.casefold()
                or own.get('name') != f'#{close.issue} relay' or agw.panes_of(own) != [mine]):
            close.log(f"leaving this relay's session open: it is not a single-pane '#{close.issue} relay' "
                      f"in workspace {close.workspace_name}")
            return
        close.log(f"closing the relay session {own.get('id')}: {summary}")
        agw.clear_restore(mine)
        agw.close_session(own.get('id'))

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
                texts = self.read_panes()
                self.check_limits(texts)
                try:
                    self.stall.tick(texts)
                except Exception as err:  # noqa: BLE001 - a watchdog bug must never stop the doorbell
                    self.log(f"stall watch failed: {type(err).__name__}: {err}")
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
    parser.add_argument("--stall-minutes", type=float,
                        help="minutes idle before a stall pointer (#45; default: stallMinutes in "
                             "~/.agworkbench.json, else 15; 0 = off)")
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
                  args.pr_interval, dry_run=args.dry_run, limit_interval=args.limit_interval,
                  stall_minutes=args.stall_minutes if args.stall_minutes is not None else stall_setting())
    try:
        return relay.run()
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
