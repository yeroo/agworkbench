#!/usr/bin/env python3
"""relay - the workbench's doorbell and its eye on GitHub.

Six jobs, one loop, one process per issue, running in its own visible agwinterm session:

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
   stop after a MERGED PR; a CLOSED unmerged PR leaves the relay watching.
   A checkout launched with -WaitOnLimit (`onLimit: "wait"` in state/implementer.json, re-read on
   every check, #77) waits a `limited` episode out instead: the planner gets one `... waiting` note
   that asks for nothing, the pane's status goes idle, and mail is held silently. Every
   `limitRetryMinutes` (config, default 30) the relay types one probe pointer into the idle agent: a
   limit that has reset lets the agent continue and the row leaves the pane, which ends the episode;
   one that has not answers with its limit again, and the wait goes on. Only a pane nobody can type
   into for `limitRetryMinutes` reaches the human. A Codex warning chooser keeps the failover path.
   A wait can also be forced (#88): by the stall watch, for a Kimi implementer whose frame ends in a
   limit error the classifier could not place, or by the planner's `wb.py wait-limit`. A forced episode
   ends only after a probe. For Kimi, whose message gives no reset time, a probe also comes 5 h after
   the relay first saw it busy in the current window (`limitWindows`), when that is sooner.

4. **The close after merge or a no-op issue (#27, #33, #53).** On an autonomous checkout, after a MERGED PR's final
   notices are delivered or a closed issue has a no-PR done record, it runs closer.py while it keeps delivering mail. Helper sessions close
   first, each on its own evidence (its completion marker, an ended direct-mode pane showing exactly
   the marker's rows), whatever the agents are doing. The gates - the planner has recorded
   `loop-state done`, no mail is unread, both agent panes are provably idle - apply to the issue
   session and the relay's own session only. Every step goes to `.workbench/state/relay-close.log`;
   a stop request, unread mail to the planner (a human's above all) or autonomy turned off stops
   it. It never closes on a timeout
   alone: mail the implementer need not act on (its final notices, anything sent after the merge)
   is ignored, and after the wait only other unread implementer mail is overridden (#44).
   A pending close survives a restart (`close_pending`, with the merge time `close_merged_at`). A
   queue member's close that gives up is handed to the conductor (#44), but only while the queue's
   conductor is running and the relay's session is provably its own: the relay keeps
   `close_pending`, records `close_handoff` and closes its own session, so the conductor's backstop
   retries it. That ends the relay, also when it resumed the close after a restart (where it used to
   carry on); a merged, retired PR leaves it nothing else to watch.

5. **Stalls (#45).** On the same reads it watches for a loop that sits idle with nothing to wake it:
   both agent panes provably idle, no unread mail (mail held for a Kimi implementer whose pane shows
   its usage limit does not count, #88), no running helper, and the loop not done, not
   waiting on the human (`state/waiting.json`, loop.json `blocked`/`pr-open`, a PR open for review),
   not waiting on CI (an auto-merge PR with a check still running), no usage-limit episode, and no
   review round waiting out a reviewer's limit (`state/review-limit.json` before its retryAt, #77).
   After `stallMinutes` (config, default 15) it mails the planner one `stall` pointer; after two more
   periods with no progress it reports the loop blocked (blocked status and sound, waiting.json, and
   loop.json in queue mode). Progress - a commit, mail, a helper, a loop report - resets it. It
   never types anything but the mail pointer, and never answers a prompt.

6. **Finished helpers (#84).** Every `--limit-interval` seconds, on every loop (autonomous or not,
   watching or draining), it closes each `#N revmux rK` and `#N suite <label>` session whose
   completion marker names a result mail that has been read - whatever its pane shows. The human's
   revdiff (`#N your review`) stays. Reports and logs in `.workbench/review/` stay; each close is
   logged in `.workbench/state/relay-close.log`. `closeHelpers: false` turns it off.

Nothing here polls on behalf of an agent: agents are woken by the relay and otherwise idle. The
relay itself polls the mailbox directory, the GitHub API, the two agent panes (for limits and
stalls), and for stalls also the terminal's session tree and `git rev-parse HEAD`, none of which
can push to it.

Typing into a pane goes through `peerchat` (vendored, fail-closed): a composer that is not
provably empty, a dialog on screen, or a pane that is not an agent is a refusal, never a send. A
refusal before typing is retried on the next tick. Submit keys are verified and retried by
peerchat; a failed ring is announced only after a later send succeeds from an empty composer, or
after the pointer rescue (#96) submits it. Every `--limit-interval` seconds, watching and draining,
and on every pass of the autonomous close, the relay looks for its own pointer still sitting in an
idle composer (the exact text it types, for mail still unread, seen the same on two looks) and
presses the submit key once more - one key per look, never text. After 3 attempts it logs
`UNSUBMITTED` and alerts. In the autonomous close a pointer whose mail no longer needs reading is
cleared with Ctrl+U instead (`closer.Closer.clear_stale_pointers`). Stall pointers and the
usage-limit probe carry no `[id X]` and are not rescued.
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
import tslog  # noqa: E402

PR_FIELDS = ("number,url,state,createdAt,updatedAt,closedAt,reviewDecision,mergedAt,reviews,comments,headRefName,"
             "isCrossRepository,statusCheckRollup,headRefOid")
HOLD_ALERT_AFTER = 60.0
AMBIGUOUS_ALERT_AFTER = 600.0
ALERT_EVERY = 300.0
TERMINAL_DRAIN_TIMEOUT = 30 * 60.0
LIMIT_READS = 2          # consecutive reads that start (or end) a usage-limit episode
STALL_MINUTES = 15.0     # the default stall period (#45); `stallMinutes` in ~/.agworkbench.json, 0 = off
# A pending set unchanged this long is stuck, not running (#94 r1): wait-ci's default --timeout, the
# bound the CI-running exemption had while every planner ran wait-ci.
CI_STUCK_MINUTES = 90.0
# A head with no check reported this long gets its `ci` mail (#94 r2): wait-ci's default --no-ci-grace,
# after which wait-ci called a head without CI done.
NO_CI_GRACE_MINUTES = 5.0
LIMIT_RETRY_MINUTES = 30.0   # between probes of an agent waiting out its limit (#77); `limitRetryMinutes`
KIMI_WINDOW_SECONDS = 5 * 3600.0   # Kimi's usage window (#88): its limit gives no reset time
WINDOW_SLACK = 60.0      # the window-end probe comes this long after the window's end
PROBE_TEXT = ("the usage limit may have reset; continue where you left off "
              "(git status, .workbench, unread mail)")
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


def gh_call(*args: str) -> Any:
    return gh_json(list(args))


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


def close_helpers_setting() -> tuple[bool, str]:
    """(`closeHelpers` from ~/.agworkbench.json, why it is off) (#84). On when the file or the key is
    missing, or the key is null (the launcher skips null too). Fails closed: a config it cannot read or
    parse (locked, half-written, `//` comments), or a value that is not true or false, closes nothing.
    The key is matched in any case, as PowerShell's launcher reads it: any spelling set to anything but
    true or null turns closing off, so `"CloseHelpers": false` or two spellings that disagree keep all."""
    path = Path(os.environ.get("AGWORKBENCH_CONFIG") or (Path.home() / ".agworkbench.json"))
    try:
        config = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return True, ''
    except (OSError, ValueError) as err:
        return False, f'config {path} unreadable: {err}'
    if not isinstance(config, dict):
        return False, f'config {path} is not a JSON object'
    for key, value in config.items():
        if key.casefold() == "closehelpers" and value is not None and value is not True:
            return False, f'{key}: {json.dumps(value)}'
    return True, ''


def limit_retry_setting() -> float:
    """`limitRetryMinutes` from ~/.agworkbench.json (#77): a number > 0. The launcher refuses an invalid
    value; one that slips through here reads as the default."""
    path = Path(os.environ.get("AGWORKBENCH_CONFIG") or (Path.home() / ".agworkbench.json"))
    try:
        config = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return LIMIT_RETRY_MINUTES
    value = config.get("limitRetryMinutes") if isinstance(config, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return LIMIT_RETRY_MINUTES
    return float(value)


def clock_text(epoch: float) -> str:
    return time.strftime("%H:%M", time.localtime(epoch))


def check_kind(item: dict[str, Any]) -> str:
    return item.get("__typename") or ("CheckRun" if "status" in item else "StatusContext")


def ci_pending(pr: dict[str, Any]) -> list[str]:
    """The PR's checks that are still running, from the snapshot's statusCheckRollup (#45 r1): a
    check run not COMPLETED, a commit status PENDING or EXPECTED."""
    pending = []
    for item in pr.get("statusCheckRollup") or []:
        if not isinstance(item, dict):
            continue
        kind = check_kind(item)
        if kind == "CheckRun" and item.get("status") != "COMPLETED":
            pending.append(str(item.get("name") or "?"))
        elif kind == "StatusContext" and item.get("state") in ("PENDING", "EXPECTED"):
            pending.append(str(item.get("context") or "?"))
    return pending


def ci_result(pr: dict[str, Any] | None) -> dict[str, Any] | None:
    """The finished CI on an OPEN PR's head (#94), or None while it is not OPEN, has no head, no check
    has reported or any check still runs. `key` names this finished run of this PR's head: a rerun or
    a check that registered late gives a new one, a repeated poll the same one. The counts take every
    check; merge-check alone knows which are required."""
    if not isinstance(pr, dict) or pr.get("state") != "OPEN" or not pr.get("headRefOid"):
        return None
    items = [item for item in pr.get("statusCheckRollup") or [] if isinstance(item, dict)]
    if not items or ci_pending(pr):
        return None
    passed, skipped, failed, rows = 0, 0, [], []
    for item in items:
        kind = check_kind(item)
        if kind == "CheckRun":
            name, verdict = item.get("name"), item.get("conclusion")
            when, link = item.get("completedAt"), item.get("detailsUrl")
            bucket = ("passed" if verdict == "SUCCESS" else "skipped" if verdict in ("NEUTRAL", "SKIPPED")
                      else "failed")
        else:
            name, verdict = item.get("context"), item.get("state")
            when, link = item.get("startedAt"), item.get("targetUrl")
            bucket = "passed" if verdict == "SUCCESS" else "failed"
        name = str(name or "?")
        if bucket == "passed":
            passed += 1
        elif bucket == "skipped":
            skipped += 1
        else:
            failed.append((name, str(link or "")))
        rows.append([str(kind), name, str(verdict or ""), str(when or ""), str(link or "")])
    head = str(pr["headRefOid"])
    key = hashlib.sha256(json.dumps([pr.get("number"), head, sorted(rows)], sort_keys=True)
                         .encode("utf-8")).hexdigest()
    names = [name for name, _ in failed]
    summary = (f"CI finished on {head[:7]}: {passed} passed, {len(failed)} failed"
               + (f" ({', '.join(names)})" if failed else "") + (f", {skipped} skipped" if skipped else ""))
    return {"head": head, "key": key, "passed": passed, "failed": names, "failed_links": failed,
            "skipped": skipped, "summary": summary}


def git_head(root: Path) -> str | None:
    try:
        done = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True,
                              timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def _last_paragraph(above: list[str], skip) -> str | None:
    """The last block of `above`, past trailing blank rows and rows `skip` matches, clipped from the
    front to LAST_WORDS_MAX."""
    above = list(above)
    while above and (not above[-1].strip() or skip(above[-1])):
        above.pop()
    start = len(above)
    while start and above[start - 1].strip():
        start -= 1
    words = " ".join(row.strip() for row in above[start:])
    if not words:
        return None
    return words if len(words) <= LAST_WORDS_MAX else "…" + words[-LAST_WORDS_MAX:]


def last_words(text: str | None, tool: str) -> str | None:
    """What an agent last said (#45): the last paragraph above its composer box - not its footer or
    status line, which are the pane's real last rows. None when the composer cannot be found."""
    import peerchat
    if tool == "kimi":
        # Kimi's box (#65); a running turn's spinner row and the todo panel (#88) sit between the answer
        # and the box.
        above = peerchat.kimi_above(text or "")
        if above is None:
            return None
        return _last_paragraph(above, lambda row: bool(peerchat.KIMI_SPINNER_RE.match(row)))
    lines = (text or "").splitlines()
    prompt_re = peerchat.CLAUDE_PROMPT_RE if tool == "claude" else peerchat.CODEX_PROMPT_RE
    prompt = next((i for i in range(len(lines) - 1, -1, -1) if prompt_re.match(lines[i])), None)
    if prompt is None:
        return None
    rule = next((i for i in range(prompt - 1, -1, -1) if peerchat.RULE_RE.match(lines[i])), None)
    if rule is None:
        return None
    # Claude's turn timer ("✻ Brewed for 1m 0s") sits between the answer and the box.
    return _last_paragraph(lines[:rule], lambda row: row.lstrip().startswith("✻"))


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
        self.held: list[str] = []                # unread implementer mail held by its usage limit (#88)
        self.ci_wait: tuple[tuple, float] | None = None   # (PR, head, pending checks), unchanged since (#94 r1)
        self.ci_stuck: str | None = None         # the pointer's note on CI stuck pending
        self.ci_stuck_logged: str | None = None
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

    def exemptions(self, live: list[str], texts: dict[str, Any] | None = None) -> list[str]:
        """Why this loop is not stalled although it may look idle. Empty when nothing exempts it. Unread
        mail to an implementer whose pane shows its limit wakes nothing (#88, docxy #820): the relay rang
        it and its turn failed. That mail is `held`, not an exemption; the stall period still applies."""
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
                # Under auto-merge the planner waits on CI: for the relay's `ci` mail or a wait-ci. A pending
                # set unchanged for CI_STUCK_MINUTES is stuck (an offline runner, a status that never reports),
                # and no `ci` mail will come: the exemption ends, as wait-ci's timeout ended its wait.
                pending = ci_pending(pr)
                current = (pr.get("number"), pr.get("headRefOid"), tuple(sorted(pending)))
                if self.ci_wait is None or self.ci_wait[0] != current:
                    self.ci_wait = (current, now())
                waited = now() - self.ci_wait[1]
                if waited < CI_STUCK_MINUTES * 60:
                    self.ci_stuck = None
                    reasons.append(f"PR #{pr.get('number')} CI running: {', '.join(pending)}")
                else:
                    stuck = f"CI on PR #{pr.get('number')} stuck: {', '.join(pending)} pending"
                    self.ci_stuck = f"{stuck}, unchanged for {waited / 60:.0f} min"
                    if stuck != self.ci_stuck_logged:
                        # Logged once: note() would repeat it, alternating with tick's "not stalled" note.
                        self.ci_stuck_logged = stuck
                        self.relay.log(f"stall watch: {self.ci_stuck}; not an exemption")
        if not (pr.get("state") == "OPEN" and ci_pending(pr)):
            self.ci_wait, self.ci_stuck, self.ci_stuck_logged = None, None, None
        unread, held = self.unread(), []
        if any(entry.startswith("codex/") for entry in unread) and texts is not None and self.implementer_limit(texts):
            held = [entry for entry in unread if entry.startswith("codex/")]
            unread = [entry for entry in unread if entry not in held]
        if held and held != self.held:
            self.relay.log(f"stall watch: unread mail {', '.join(held)} is held for the limited implementer; "
                           "not an exemption")
        self.held = held
        if unread:
            reasons.append(f"unread mail {', '.join(unread)}")
        if live:
            reasons.append(f"helper running: {', '.join(live)}")
        if self.relay.state.get("limits"):
            reasons.append(f"usage-limit episode: {', '.join(sorted(self.relay.state['limits']))}")
        review = self.read_json("review-limit.json")
        if isinstance(review, dict):
            # #77: a reviewer's limit stopped a revmux round; the planner waits for the rerun. Past its
            # retryAt the rerun's own session is the helper that exempts the loop, or the loop is stalled.
            retry = review.get("retryAt")
            if isinstance(retry, bool) or not isinstance(retry, (int, float)):
                reasons.append("review usage-limit wait (state/review-limit.json)")
            elif wall() < retry:
                reasons.append(f"review usage-limit wait until {clock_text(retry)}")
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
        exempt = self.exemptions(live, texts)
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
            if instant - self.since >= self.period:
                if self.wait_out_limit(texts):
                    self.reset("usage limit")
                elif self.ci_backstop():
                    self.reset("CI mail queued")
                elif self.pointer(instant - self.since, quiet, texts):
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
    def implementer_limit(self, texts: dict[str, Any]) -> tuple[Peer, limits.Limit] | None:
        """#88: a Kimi implementer whose frame ends in a limit error the limit check could not place (the
        error glued under a tool call): the idle stall period is the evidence the owner rule stood in for."""
        for peer in self.relay.peers:
            text = texts.get(peer.box)
            if peer.box == "codex" and peer.tool == "kimi" and isinstance(text, str):
                found = limits.kimi_turn_limit(text)
                if found:
                    return peer, found
        return None

    def wait_out_limit(self, texts: dict[str, Any]) -> bool:
        """In a -WaitOnLimit checkout a stalled implementer at its limit is waited out (#88), not pointed
        at the planner: True when the relay waits now. The episode exempts the loop while it lasts."""
        limited = self.implementer_limit(texts)
        if not limited or self.relay.on_limit() != "wait":
            return False
        peer, found = limited
        self.relay.force_wait(peer, found, texts[peer.box], "stall")
        return True

    def ci_backstop(self) -> bool:
        """#94: finished CI on the open PR's head that no `ci` mail named yet is the stall: queue that mail
        instead of the pointer. Defensive: watch_pr queues it in the same save as the snapshot, so this is
        reached only when `ci_mailed` was lost while the snapshot survived (hand-edited state). Only a
        finished `ci_result`: the time-based no-checks mail comes from a fresh watch_pr snapshot alone, never
        from a saved one that gh failures left stale (#94 r3). True when the mail was queued (its flush may
        still be retried)."""
        pr = self.relay.state.get("pr")
        return bool(pr) and ci_result(pr) is not None and self.relay.ci_mail(pr)

    def implementer_line(self, texts: dict[str, Any]) -> str | None:
        for peer in self.relay.peers:
            if peer.box == "codex" and isinstance(texts.get(peer.box), str):
                return last_words(texts[peer.box], peer.tool)
        return None

    def pointer(self, idle: float, quiet: list[str], texts: dict[str, Any]) -> bool:
        """File the stall pointer. False when it could not be filed: the level stays, and the next
        tick tries again - an escalation never cites a pointer that does not exist."""
        minutes = f"{idle / 60:.0f}"
        held = list(self.held)
        subject = (f"stall: loop idle for {minutes} min, " + ("mail held for the limited implementer" if held
                   else "nothing unread") + ", no running helper")
        mail = "no unread mail but what is held for the implementer" if held else "no unread mail in either box"
        body = [f"The relay has seen this loop idle for {minutes} minutes: both agent panes idle with an empty",
                f"composer, {mail}, no running helper, no PR open for review or CI",
                "running, no usage-limit episode, and the loop neither done nor waiting on the human.", ""]
        notes = [f"unread mail {entry} is held for the implementer at its usage limit" for entry in held] + quiet
        if self.ci_stuck:
            notes.append(f"{self.ci_stuck} - look at the checks; a runner or an external CI may be down")
        body += [f"- {line}" for line in notes] + ([""] if notes else [])
        words = self.implementer_line(texts)
        if words:
            body += [f"The implementer's last line: {words}", ""]
        limited = self.implementer_limit(texts)
        if limited:
            body += [f"The implementer's pane shows a usage-limit error: {limited[1].line}", ""]
        ci = ci_result(self.relay.state.get("pr"))
        if ci:
            body += [f"CI on the PR head: {ci['summary']}", ""]
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
                   + (f"; {self.ci_stuck}" if self.ci_stuck else "") + (f"; {quiet[0]}" if quiet else ""))
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
        # The finished-helper sweep (#84): built on its first use, kept so it logs each decision once.
        self.sweeper: closer.Closer | None = None
        self.sweep_note: str | None = None
        self.dry_run = dry_run
        self.state_file = hub_dir / "state" / "relay.json"
        self.stop_file = hub_dir / "state" / "relay.stop"
        self.state = self._load()
        saved_pr = self.state.get('pr')
        saved_branch = self.state.get('branch', (saved_pr or {}).get('headRefName'))
        changed = self.state.get('branch') != self.branch
        if saved_branch != self.branch or (saved_pr and saved_pr.get('headRefName') != self.branch):
            branch_keys = ('pr', 'terminal_mail', 'ignored_prs', 'seen_open', 'completed_prs',
                           'watch_since', 'outbox', 'event_sequence', 'ci_mailed')
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
        self._dry_ci_key = None
        self.no_checks: tuple[tuple, float] | None = None   # (PR, head) seen with an empty rollup, since (#94 r2)
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
        # #96: a pointer left typed-but-unsent in a composer gets submitted on a later look.
        self.rescue = closer.PointerRescue(
            self.peers, unread_message=self.unread_message, log=lambda text: self.log(text),
            pointer=lambda message: closer.relay_pointer(message, self.agmsg, self.hub_dir),
            alert=lambda peer, mid, reason: self.alert(peer, mid, reason),
            resolved=lambda peer, mid: self.rescue_resolved(peer), clock=lambda: now())
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
        for attempt in range(20):
            try:
                os.replace(tmp, self.state_file)
                return
            except PermissionError:
                # Windows refuses the replace while another process (the conductor) is reading it.
                if attempt == 19:
                    raise
                time.sleep(0.05)

    def log(self, text: str) -> None:
        print(text, flush=True)     # main() installed the timestamp prefix (#78)

    # mail -----------------------------------------------------------------------------------
    def hold(self, peer: Peer, mid: str, reason: str, *, failed: bool = False,
             ambiguous_text: str | None = None, quiet: bool = False) -> None:
        """Hold one message for a recipient that is not ready. A quiet hold (a usage limit waited out,
        #77) never alerts: the wait is the plan, not a problem for the human."""
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
        if not quiet and (failed or instant - since >= threshold) and (
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
        is announced once. A forced episode (#88: the stall watch's or the planner's) is waited out even
        when nothing on screen is placed: only its probe can end it."""
        self.take_limit_request(texts)
        episodes = dict(self.state.get('limits', {}))
        changed = False
        for peer in self.peers:
            text = texts.get(peer.box)
            if not isinstance(text, str):
                self.log(f"limit check: cannot read {peer.box}: {text}")
                continue
            found = limits.classify(text, peer.tool)
            episode = episodes.get(peer.box)
            if episode is None and self.note_window(peer.box, text):
                changed = True
            forced = bool(episode and episode.get('forced'))
            if peer.box not in self.limit_baseline:
                # Only an old `limited` row is history. A warning is Codex's modal chooser, live at
                # the bottom of the pane: it never scrolls away, so a baseline would hide it (#61).
                continuing = bool(found and episode and episode.get('line') == found.line)
                old = found and found.kind == 'limited' and not continuing
                self.limit_baseline[peer.box] = {found.line} if old else set()
                if self.limit_baseline[peer.box]:
                    self.log(f"limit check: ignoring {peer.box}'s limit row already on screen at start: {found.line}")
            baseline = self.limit_baseline[peer.box]
            if found and found.line in baseline and not forced:
                found = None
            elif not found and baseline and not any(line in text for line in baseline):
                baseline.clear()
            if not found and forced and peer.tool == 'kimi':
                found = limits.kimi_turn_limit(text)
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
                        # Only a hard limit is waited out (#77): nobody answers a warning chooser.
                        if found.kind == 'limited' and self.on_limit() == 'wait':
                            self.start_wait(peer, episode)
                        self.announce_limit(peer, episode, text)
                        episode['announced'] = True
                elif episode.get('wait') and episode['announced'] and not self.dry_run:
                    self.wait_limit(peer, episode)
                changed = True
            elif episode:
                if forced and episode.get('probeAt') is None and not episode.get('probes'):
                    # Nothing the relay can place is on screen; the stall watch or the planner saw the
                    # limit (#88). Only the first probe's answer can tell that it has reset.
                    episode['misses'] = 0
                    if episode.get('announced') and not self.dry_run:
                        self.wait_limit(peer, episode)
                elif episode.get('wait') and episode.get('probeAt') is not None and is_busy(text):
                    # The agent took the probe and is working (#77): its limit row may yet come back.
                    episode['misses'] = 0
                else:
                    episode['misses'] = episode.get('misses', 0) + 1
                if episode['misses'] >= LIMIT_READS:
                    episodes.pop(peer.box)
                    self.state.get('limitWindows', {}).pop(peer.box, None)
                    if episode.get('wait') and episode.get('probeAt') is not None:
                        self.log(f"usage limit episode ended for {peer.box}: the limit reset "
                                 f"(probe {episode.get('probes', 0) + 1})")
                    else:
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
        rows = [row for row in text.splitlines() if row.strip()][-limits.WINDOW:]
        if episode.get('wait'):
            self.announce_wait(peer, episode, rows)
            return
        subject = f"usage limit: {peer.box} ({peer.tool}) {episode['kind']}"
        if peer.box == 'claude':
            step = ("The planner itself is limited, so nobody can act on this mail until it can: "
                    "the human has been notified. The loop waits.")
        else:
            # A warning chooser fails over like the hard limit (#61): nobody answers it.
            if episode['kind'] == 'warning':
                what = "shows Codex's usage-warning chooser (it is nearly limited). Never answer the chooser"
            else:
                what = "has hit its usage limit" + (" and exited to a shell" if episode.get('exited') else "")
            step = (f"The implementer {what}. Follow start-github-issue.md, Usage limits: check the frame "
                    "below, then fail over with `github-workbench.cmd <issue> -Failover` (Bash timeout "
                    "600000) when failover is on.")
        body = "\n".join([f"Matched: {episode['line']}", f"Pane: {peer.box} ({peer.tool}) {peer.pane}",
                           f"First seen: {episode['firstSeen']}", "", "Next step: " + step, "",
                           "Last rows of the pane:", "", "```", *rows, "```"])
        self.alert_limit(peer, subject, body, episode['line'])

    def alert_limit(self, peer: Peer, subject: str, body: str, detail: str) -> None:
        """A limit the human hears about: the planner's note, then the pane blocked with sound, and a
        notification."""
        import agw
        try:
            self.hub.write_message(to='claude', sender='relay', kind='note', subject=subject, body=body)
        except OSError as err:
            self.log(f"could not file usage-limit mail: {err}")
        self.log(f"ALERT {subject}: {detail}")
        try:
            agw.set_status('blocked', sound=True, blink=True, pane_id=peer.pane)
        except (agw.CtlError, OSError) as err:
            self.log(f"could not set blocked status for {peer.box}: {err}")
        try:
            agw.notify(peer.pane, f"{subject}: {detail}", title='workbench relay')
        except (agw.CtlError, OSError) as err:
            self.log(f"could not notify {peer.box}: {err}")

    # waiting out a usage limit (#77) ------------------------------------------------------------
    def on_limit(self) -> str:
        """The checkout's `onLimit` (state/implementer.json), read on every check: 'wait' or 'failover'."""
        try:
            saved = json.loads((self.hub_dir / "state" / "implementer.json").read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return 'failover'
        return 'wait' if isinstance(saved, dict) and saved.get('onLimit') == 'wait' else 'failover'

    def retry_seconds(self) -> float:
        return limit_retry_setting() * 60

    def note_window(self, box: str, text: str) -> bool:
        """#88: when the agent was first seen busy in its current usage window - after the relay started,
        after its last limit episode, or 5 h after the last start. True when it changed."""
        if not is_busy(text):
            return False
        windows = self.state.setdefault('limitWindows', {})
        start = windows.get(box)
        if isinstance(start, (int, float)) and not isinstance(start, bool) and wall() - start < KIMI_WINDOW_SECONDS:
            return False
        windows[box] = wall()
        return True

    def start_wait(self, peer: Peer, episode: dict) -> None:
        """Turn an episode into a wait (#77). A Kimi window start the relay saw gives its end (#88)."""
        start = self.state.get('limitWindows', {}).get(peer.box)
        if peer.tool == 'kimi' and isinstance(start, (int, float)) and not isinstance(start, bool):
            episode['windowEndsAt'] = start + KIMI_WINDOW_SECONDS
        episode.update(wait=True, probes=0, probeAt=None)
        episode['retryAt'] = self.next_retry(episode)

    def next_retry(self, episode: dict) -> float:
        """limitRetryMinutes from now, or just after the usage window ends when that is sooner (#88)."""
        retry = wall() + self.retry_seconds()
        ends = episode.get('windowEndsAt')
        if isinstance(ends, (int, float)) and not isinstance(ends, bool) and wall() < ends + WINDOW_SLACK < retry:
            return ends + WINDOW_SLACK
        return retry

    def force_wait(self, peer: Peer, found: limits.Limit, text: str, source: str) -> bool:
        """Start a wait episode now, without the two classified reads (#88): `source` is "stall" (the stall
        watch saw a limit error the limit check could not place) or "planner" (`wb.py wait-limit`). False
        when the box already has an episode."""
        if peer.box in self.state.get('limits', {}):
            return False
        if self.dry_run:
            self.log(f"[dry-run] would wait out {peer.box}'s usage limit ({source}): {found.line}")
            return True
        episode = self.state.setdefault('limits', {})[peer.box] = {
            'kind': 'limited', 'line': found.line, 'tool': peer.tool,
            'firstSeen': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'since': wall(), 'tail': limits.tail_hash(text), 'hits': LIMIT_READS, 'misses': 0,
            'announced': False, 'exited': False, 'forced': source}
        self.start_wait(peer, episode)
        self.log(f"usage limit: {peer.box} ({peer.tool}) waiting, forced by the {source}")
        self.announce_limit(peer, episode, text)
        episode['announced'] = True
        self._save()
        return True

    def take_limit_request(self, texts: dict[str, Any]) -> None:
        """`wb.py wait-limit` (#88): the planner saw a limit the relay did not. The request is consumed
        whatever happens to it, so a refused one is never retried."""
        path = self.hub_dir / "state" / "limit-request.json"
        if self.dry_run or not path.exists():
            return
        try:
            request = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            request = None
        try:
            path.unlink()
        except OSError as err:
            self.log(f"limit request: cannot remove {path.name}: {err}")
            return
        if not isinstance(request, dict):
            self.log("limit request: unreadable; ignored")
            return
        box = request.get('box') or 'codex'
        peer = next((p for p in self.peers if p.box == box), None)
        if peer is None:
            self.log(f"limit request: no pane for box {box!r}; ignored")
            return
        if self.on_limit() != 'wait':
            self.log("limit request: this checkout fails over (onLimit is not wait); ignored")
            return
        reason = str(request.get('reason') or 'the planner reports a usage limit').strip()
        text = texts.get(box)
        if not self.force_wait(peer, limits.Limit('limited', reason), text if isinstance(text, str) else '', 'planner'):
            self.log(f"limit request: {box} already has a usage-limit episode")

    def announce_wait(self, peer: Peer, episode: dict, rows: list[str]) -> None:
        """One note to the planner that asks for nothing, and an idle status: no sound, no notification."""
        import agw
        subject = f"usage limit: {peer.box} ({peer.tool}) waiting"
        who = "the planner (you)" if peer.box == 'claude' else "the implementer"
        step = (f"Do nothing: no failover. This checkout waits out usage limits (onLimit=wait): {who} is "
                f"limited, the relay holds its mail and asks it to continue every "
                f"{self.retry_seconds() / 60:g} min (next try {clock_text(episode['retryAt'])}), and the loop "
                "resumes when the limit has reset. Keep your mail waiter and end your turn.")
        body = "\n".join([f"Matched: {episode['line']}", f"Pane: {peer.box} ({peer.tool}) {peer.pane}",
                           f"First seen: {episode['firstSeen']}", "", "Next step: " + step, "",
                           "Last rows of the pane:", "", "```", *rows, "```"])
        try:
            self.hub.write_message(to='claude', sender='relay', kind='note', subject=subject, body=body)
        except OSError as err:
            self.log(f"could not file usage-limit mail: {err}")
        self.log(f"{subject}: {episode['line']}; next try {clock_text(episode['retryAt'])}")
        try:
            agw.set_status('idle', pane_id=peer.pane)
        except (agw.CtlError, OSError) as err:
            self.log(f"could not set idle status for {peer.box}: {err}")

    def wait_limit(self, peer: Peer, episode: dict) -> None:
        """A limit row on screen during a wait episode, or a forced episode before its first probe (#88,
        nothing placed on screen): after a probe a limit row means the limit has not reset; at retryAt it
        is time to probe."""
        if episode.get('probeAt') is not None:
            episode.update(probes=episode.get('probes', 0) + 1, probeAt=None)
            episode['retryAt'] = self.next_retry(episode)
            self.log(f"usage limit: {peer.box} still limited after probe {episode['probes']}; "
                     f"next try {clock_text(episode['retryAt'])}")
            return
        if wall() >= episode.get('retryAt', 0):
            self.probe(peer, episode)

    def probe(self, peer: Peer, episode: dict) -> None:
        """Type the one probe pointer into the limited agent's idle composer. A pane nobody can type into
        is retried on every check; after limitRetryMinutes of that the human is told, once."""
        import agw
        import peerchat
        try:
            if peer.tool in ("claude", "kimi") and is_busy(agw.pane_text(peer.pane)):
                raise peerchat.Refused('mid-turn; waiting for the agent to finish')
            text = peerchat.compose_text("Chat from Workbench: ", PROBE_TEXT)
            outcome = peerchat.send(peer.pane, peerchat.PROFILES[peer.tool], text, dry_run=False, retry=False)
        except (peerchat.Refused, peerchat.Failed, agw.CtlError, OSError) as err:
            since = episode.setdefault('probeRefusedSince', wall())
            self.log(f"usage limit: cannot probe {peer.box}: {err}")
            if wall() - since >= self.retry_seconds() and not episode.get('escalated'):
                episode['escalated'] = True
                self.escalate_wait(peer, episode, str(err))
            return
        episode.update(probeAt=wall(), misses=0)
        episode.pop('probeRefusedSince', None)
        if episode.pop('escalated', None):
            # The pane can take the probe again: the blocked status the escalation set is over.
            try:
                agw.set_status('idle', pane_id=peer.pane)
            except (agw.CtlError, OSError) as err:
                self.log(f"could not clear blocked status for {peer.box}: {err}")
        self.log(f"usage limit: probed {peer.box} (probe {episode.get('probes', 0) + 1}) [{outcome}]")

    def escalate_wait(self, peer: Peer, episode: dict, reason: str) -> None:
        """The only human-facing step of a wait: the probe could not be typed for limitRetryMinutes."""
        subject = f"usage limit: {peer.box} ({peer.tool}) waiting, cannot probe"
        body = "\n".join([f"Matched: {episode['line']}", f"Pane: {peer.box} ({peer.tool}) {peer.pane}",
                           f"First seen: {episode['firstSeen']}", "",
                           f"The relay could not type its probe pointer for {self.retry_seconds() / 60:g} min: "
                           f"{reason}. The pane is not an idle agent composer (a shell, a dialog or a draft); "
                           "the human has been notified. The relay keeps trying."])
        self.alert_limit(peer, subject, body, reason)

    # finished helpers (#84) ---------------------------------------------------------------------
    def sweep_note_once(self, text: str) -> None:
        if self.sweep_note != text:
            self.sweep_note = text
            self.log(text)

    def sweep_helpers(self) -> None:
        """One look for finished helpers to close (closer.step_finished_helpers). Never raises for a
        setting, a queue membership or a terminal it cannot read: it says so once and tries again."""
        enabled, why = close_helpers_setting()
        if not enabled:
            self.sweep_note_once(f'helper close off ({why})')
            return
        if self.sweeper is None:
            try:
                self.sweeper = self.closer()
            except ValueError as err:
                self.sweep_note_once(f'helper close skipped: {err}')
                return
        import agw
        try:
            self.sweeper.step_finished_helpers()
        except (agw.CtlError, OSError) as err:
            self.sweep_note_once(f'helper close skipped: terminal unreadable ({err})')
            return
        self.sweep_note = None

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
        """Is this checkout's queue conductor running (#44)? Under the queue's state lock: its worker
        lock is held and its owner is `running`. Under that lock because a conductor decides to finish
        under it too, after reading every member's relay.json (conductor.handed_off): a relay that saved
        close_handoff and then sees `running` here is certain the conductor will see it. The worker lock
        is probed there as `-Queue` start() does, so the probe cannot mislead a concurrent start."""
        import conductor
        try:
            member = json.loads((self.hub_dir / 'state' / 'queue-member.json').read_text(encoding='utf-8-sig'))
            store = conductor.Store(member['queue'])
            if not store.worker_lock.exists():
                return False                     # never ran, or removed: and creates nothing for it
            with conductor.Lock(store.state_lock):
                if not store.running():
                    return False
                owner = store._load().get('owner') or {}
            return owner.get('state') == 'running'
        except (OSError, KeyError, TypeError, ValueError):   # conductor.QueueError is a ValueError
            return False

    def hand_off_close(self, close: "closer.Closer", number: int | None, reasons: list[str]) -> bool:
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
        key = closer.pending_key(number)
        self.state['close_pending'] = key
        self.state['close_handoff'] = {'pr': key, 'at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                                       'reasons': reasons}
        self._save()
        if not self.conductor_running():
            # It finished between the two looks, before it could have seen the handoff.
            close.log("NOT handing the close to the queue conductor: it finished meanwhile")
            return False
        close.log(f"handing the close to the queue conductor; closing the relay session {session.get('id')}")
        try:
            agw.notify(mine, f"{'issue closed without a PR' if number is None else f'PR #{number}'}: autonomous close handed to the queue conductor", title='workbench relay')
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

    def refuse_close(self, close: "closer.Closer", number: int | None, reasons: str | list[str],
                     *, handoff: bool = True) -> None:
        """Refuse a close without letting a failed queue handoff crash the relay."""
        import agw
        items = [reasons] if isinstance(reasons, str) else reasons
        detail = '; '.join(items)
        close.log(f"NOT closing{', ' if detail.startswith('still waiting after') else ': '}{detail}")
        self.close_alert(detail)
        handed = False
        if handoff:
            try:
                handed = self.hand_off_close(close, number, items)
            except (agw.CtlError, OSError) as err:
                close.log(f'could not hand the close to the queue conductor: {err}')
        if not handed:
            self.finish_close()

    def resume_no_pr_loop(self, close: "closer.Closer", reason: str, expected_at) -> bool:
        """Disarm a refused done record, or persist its timestamp until retirement can retry."""
        import conductor
        try:
            retired = conductor.retire_no_pr_done(
                self.hub_dir / 'state' / 'loop-done.json', expected_at=expected_at, issue=close.issue)
        except (OSError, ValueError) as err:
            close.log(f'could not retire refused no-PR completion: {err}')
            self.state['refused_no_pr_at'] = expected_at
            if not self.dry_run:
                self._save()
            self.refuse_close(close, None, reason, handoff=False)
            return False
        if retired:
            close.log('preserved refused no-PR completion as loop-done-refused.json')
            if 'refused_no_pr_at' in self.state:
                self.state.pop('refused_no_pr_at')
                if not self.dry_run:
                    self._save()
        self.refuse_close(close, None, reason, handoff=False)
        return False

    def close_after_merge(self, number: int | None) -> bool:
        """After a merged PR drain, or a no-PR done record on a CLOSED issue: close the issue's
        helpers as soon as each is proven done and untouched (#33), the issue session only when both
        agents are provably done and idle, then this relay's own session. Mail keeps flowing while it
        waits; number=None requires fresh GitHub CLOSED checks before any session closes. A stop
        request, unread planner mail (a human's above all), or autonomy turned off stops it; never on a timeout
        alone (after it, only unread implementer mail is overridden, #44). A refusal is handed to the
        queue conductor's backstop when there is a running one and the session is provably ours (#44).
        A reopened issue or new open PR leaves the relay watching and returns False."""
        import agw
        try:
            close = self.closer()
        except ValueError as err:
            # Which workspace holds the sessions is unknown (#66): nothing can be proven closed.
            self.log(f"no autonomous close: {err}; the sessions stay open")
            self.finish_close()
            return True
        if not close.autonomous():
            self.log("autonomy is off for this checkout; the sessions stay open")
            self.finish_close()
            return True
        close.log(f"{'issue closed without a PR' if number is None else f'PR #{number} merged'}; autonomous close starting")
        if self.state.pop('close_handoff', None) is not None and not self.dry_run:
            # A restarted relay owns the close again; the conductor defers to it while it lives.
            self._save()
        left_open: list[str] = []
        next_issue_check = 0.0
        issue_verified = False
        issue_detail = ''
        no_pr_at = None
        saw_no_pr_done = False
        while True:
            if self.stop_file.exists():
                # The launcher is restarting this relay; the pending close resumes after it.
                close.log("NOT closing: stop requested; the close resumes when the relay restarts")
                return True
            # The planner's "loop complete" mail (and any human mail) must still be rung.
            self.flush_outbox()
            self.deliver_mail()
            if number is None:
                done_record = close.no_pr_done_record()
                if done_record is None:
                    try:
                        loop = json.loads((self.hub_dir / 'state' / 'loop.json').read_text(encoding='utf-8-sig'))
                    except (OSError, ValueError):
                        loop = {}
                    if saw_no_pr_done or (isinstance(loop, dict) and loop.get('state') in ('resumed', 'pr-open')):
                        close.log('no-PR completion was withdrawn; the relay resumes watching')
                        self.finish_close()
                        return False
                    reason = 'the planner has not recorded `wb.py loop-state done --no-pr`'
                    if close.timed_out():
                        self.refuse_close(close, number, reason)
                        return True
                    pause(self.mail_interval)
                    continue
                saw_no_pr_done = True
                if no_pr_at is None:
                    no_pr_at = done_record.get('at')
                if now() >= next_issue_check:
                    closed, detail = close.issue_closed(gh_call)
                    next_issue_check = now() + self.pr_interval
                    issue_verified = closed is True
                    issue_detail = detail
                    if closed is False:
                        return self.resume_no_pr_loop(close, f'issue #{close.issue} was reopened', no_pr_at)
                if not issue_verified:
                    if close.timed_out():
                        self.refuse_close(close, number, f'issue state unknown: {issue_detail}')
                        return True
                    pause(self.mail_interval)
                    continue
            try:
                # Helpers first, and regardless of the agents (#33).
                left_open = close.step_helpers(
                    gate=(lambda: close.issue_closed(gh_call)[0] is True) if number is None else None)
            except (agw.CtlError, OSError) as err:
                close.log(f"helper check failed: {err}")
            try:
                # #96: a pointer for mail still unread is submitted; one nobody needs to read is cleared.
                self.rescue_pointers(skip=lambda peer, message: close.needs_no_reading(number, peer, message))
                self.rung(close.clear_stale_pointers(number))
            except Exception as err:  # noqa: BLE001 - the blockers below still decide
                close.log(f"pointer check failed: {type(err).__name__}: {err}")
            reasons = close.agent_blockers(number)
            if not reasons or close.overdue_ok():
                if number is None:
                    closed, detail = close.issue_closed(gh_call)
                    if closed is False:
                        return self.resume_no_pr_loop(close, f'issue #{close.issue} was reopened', no_pr_at)
                    if closed is None:
                        issue_verified = False
                        issue_detail = detail
                        next_issue_check = now() + self.pr_interval
                        if close.timed_out():
                            self.refuse_close(close, number, f'issue state unknown: {detail}')
                            return True
                        pause(self.mail_interval)
                        continue
                    open_pr, detail = close.open_prs(gh_call, self.branch)
                    if open_pr is True:
                        return self.resume_no_pr_loop(close, detail, no_pr_at)
                    if open_pr is None:
                        issue_verified = False
                        issue_detail = detail
                        next_issue_check = now() + self.pr_interval
                        if close.timed_out():
                            self.refuse_close(close, number, detail)
                            return True
                        pause(self.mail_interval)
                        continue
                    current_done = close.no_pr_done_record()
                    if current_done is None or current_done.get('at') != no_pr_at:
                        close.log('no-PR completion changed during the close; the relay resumes watching')
                        self.finish_close()
                        return False
                break
            if close.timed_out():
                self.refuse_close(close, number, f"still waiting after {closer.CLOSE_WAIT:.0f}s: " + '; '.join(reasons))
                return True
            pause(self.mail_interval)
        if not close.autonomous():
            close.log("NOT closing: autonomy was turned off during the wait")
            self.finish_close()
            return True
        if self.dry_run:
            close.log('[dry-run] would close the issue session and the relay')
            return True
        try:
            close.close_issue_session()
            # Before this relay's own session goes (that ends this process), and detached from it (#41).
            close.start_cleanup(number)
            self.close_own_session(close, number, left_open)
        except (agw.CtlError, OSError) as err:
            close.log(f"close failed: {err}")
            self.close_alert(f"a close step failed: {err}")
            self.finish_close()
        return True

    def close_own_session(self, close: "closer.Closer", number: int | None, left_open: list[str]) -> None:
        import agw
        summary = (f"{'issue closed without a PR' if number is None else f'PR #{number} merged'}; sessions closed"
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

    def unread_message(self, box: str, mid: str) -> dict[str, Any] | None:
        """One unread message of a box by id, parsed; None when it is not unread (any more)."""
        for path in self.hub.unread(box):
            if path.stem != mid:
                continue
            try:
                return self.hub.parse_message(path)
            except (FileNotFoundError, ValueError):
                return None
        return None

    def rescue_pointers(self, skip=None) -> None:
        """One pointer-rescue look at both panes (#96). A rescued mid is rung (`rung`). `skip(peer,
        message)` leaves a pointer alone: the close's mail that needs no reading."""
        if self.dry_run:
            return
        self.rung(self.rescue.step(self.read_panes(), skip=skip))

    def rung(self, pointers) -> None:
        """Count each (peer, mid) whose pointer is dealt with - rescued, or cleared by the close - as
        rung: announced (saved, so neither deliver_mail nor a restart types it again), hold cleared."""
        if not pointers:
            return
        announced = set(self.state.get("announced", []))
        for peer, mid in pointers:
            announced.add(mid)
            self.clear(peer, mid)
        self.state["announced"] = sorted(announced)
        self._save()

    def rescue_resolved(self, peer: Peer) -> None:
        """The rescue's UNSUBMITTED alert for this pane is over: take its blocked status back, unless a
        hold of ours still has it alerted (#96 r1 m1: the alert is the rescue's, not a hold's, so the
        announced-mail sweep in deliver_mail cannot reset it while the pointer is still stuck)."""
        import agw
        if self.dry_run or any(box == peer.box and held.alerted for (box, _), held in self.holds.items()):
            return
        try:
            agw.set_status('idle', pane_id=peer.pane)
        except (agw.CtlError, OSError) as err:
            self.log(f"could not clear relay status for {peer.box}: {err}")

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
                if (episode and episode.get('kind') in ('limited', 'warning') and episode.get('announced')
                        and not self.draining):
                    if episode.get('wait'):
                        self.hold(peer, mid, 'usage limit, waiting it out', quiet=True)
                    else:
                        self.hold(peer, mid, 'usage limit')
                    continue
                try:
                    # Claude and Kimi take Return, which would land in a running turn: ring between turns.
                    if peer.tool in ("claude", "kimi") and is_busy(agw.pane_text(peer.pane)):
                        raise peerchat.Refused('mid-turn; waiting for the agent to finish')
                    text = closer.relay_pointer(message, self.agmsg, self.hub_dir)
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
        self.state.pop('ci_mailed', None)
        if self.state.get('close_pending') == number:
            self.state.pop('limits', None)  # a merged PR ends the loop
        self.log(f'retired finished PR #{number}; the relay can watch the next PR if this one was unmerged')
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

    def ci_mail(self, snapshot: dict[str, Any], outbox: list[dict[str, Any]] | None = None) -> bool:
        """#94: mail the planner once per finished CI run on the PR's head (`ci_result`'s key), or once for
        a head with no check past the grace (`no_checks_result`'s key), so a planner whose wait-ci was
        killed is still woken. Queued on `outbox` when given (watch_pr saves
        it with the snapshot); otherwise appended to the saved outbox and flushed. The id is fixed, so a
        retried or replayed write never duplicates it. True when the mail was queued."""
        result = ci_result(snapshot) or self.no_checks_result(snapshot)
        if result is None or result["key"] == self.state.get("ci_mailed"):
            return False
        if self.dry_run:
            if result["key"] != self._dry_ci_key:
                self._dry_ci_key = result["key"]
                self.log(f"[dry-run] would mail the planner: {result['summary']}")
            return False
        number = snapshot["number"]
        body = [f"CI finished on PR #{number} ({snapshot.get('url') or '?'}), head {result['head']}:"]
        if result.get("no_checks"):
            body += [f"no check reported for this head in {NO_CI_GRACE_MINUTES:g} minutes (wait-ci's no-CI grace).", ""]
        else:
            body += [f"{result['passed']} passed, {len(result['failed'])} failed, {result['skipped']} skipped.", ""]
        if result["failed_links"]:
            body += ["Failed:"] + [f"- {name}" + (f": {link}" if link else "")
                                   for name, link in result["failed_links"]] + [""]
        body += ["The counts take every check; merge-check decides which are required - do not act on the",
                 "count.", "",
                 f"Under auto-merge: if {result['head']} is the head you tested, stop any wait-ci still running",
                 f"for it and run `wb.py merge-check --pr {number} --head {result['head']}`, as on wait-ci's exit 0.",
                 "A `ci` mail for another head is ignored.",
                 "Without auto-merge: only mention a red result in chat."]
        message = dict(to="claude", sender="relay", subject=result["summary"], body="\n".join(body), kind="ci",
                       message_id=f"relay-ci-pr{number}-{result['key'][:8]}")
        self.state["ci_mailed"] = result["key"]
        self.log(f"CI mail to the planner: {result['summary']}")
        if outbox is not None:
            outbox.append(message)
            return True
        self.state["outbox"] = [*self.state.get("outbox", []), message]
        self._save()
        self.flush_outbox()
        return True

    def no_checks_result(self, snapshot: dict[str, Any]) -> dict[str, Any] | None:
        """#94 r2: an OPEN PR's head whose rollup stayed empty for NO_CI_GRACE_MINUTES - a repo without CI, or
        a head no workflow runs for - is done, as wait-ci's no-CI grace says. The clock starts at this
        relay's first sight of that head with no check, so a restart only delays it."""
        items = [item for item in (snapshot or {}).get("statusCheckRollup") or [] if isinstance(item, dict)]
        if not snapshot or snapshot.get("state") != "OPEN" or not snapshot.get("headRefOid") or items:
            self.no_checks = None
            return None
        head = str(snapshot["headRefOid"])
        current = (snapshot.get("number"), head)
        if self.no_checks is None or self.no_checks[0] != current:
            self.no_checks = (current, now())
        if now() - self.no_checks[1] < NO_CI_GRACE_MINUTES * 60:
            return None
        key = hashlib.sha256(json.dumps([snapshot.get("number"), head, "no-checks"]).encode("utf-8")).hexdigest()
        return {"head": head, "key": key, "passed": 0, "failed": [], "failed_links": [], "skipped": 0,
                "no_checks": True,
                "summary": f"CI finished on {head[:7]}: no checks reported in {NO_CI_GRACE_MINUTES:g} min"}

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
        if snapshot.get('state') == 'OPEN':
            self.ci_mail(snapshot, outbox)
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
                self.state['outbox'] = [*self.state.get('outbox', []), *outbox]
            self._save()
            self.flush_outbox()
        return terminal

    def pending_terminal_mail(self) -> set[tuple[str, str]]:
        announced = set(self.state.get('announced', []))
        targets = {tuple(target) for target in self.state.get('terminal_mail', [])}
        unread = {(box, path.stem)
                  for box in {box for box, _ in targets} for path in self.hub.unread(box)}
        return {(box, mid) for box, mid in targets & unread if mid not in announced}

    def no_pr_close_due(self) -> str | None:
        """Return a safe done time. An open PR or issue retires the stale record as a live loop."""
        import conductor
        try:
            close = self.closer()
        except ValueError as err:
            self.log(f"no-PR close cannot start: {err}")
            return None
        if not close.no_pr_done():
            return None
        try:
            done = json.loads((self.hub_dir / 'state' / 'loop-done.json').read_text(encoding='utf-8-sig'))
            at = datetime.fromtimestamp(done['at'], timezone.utc).isoformat()
        except (OSError, ValueError, OverflowError, KeyError, TypeError):
            return None
        if 'refused_no_pr_at' in self.state:
            if done['at'] == self.state['refused_no_pr_at']:
                try:
                    if conductor.retire_no_pr_done(self.hub_dir / 'state' / 'loop-done.json',
                                                   expected_at=done['at'], issue=close.issue):
                        self.state.pop('refused_no_pr_at', None)
                        if not self.dry_run:
                            self._save()
                except (OSError, ValueError) as err:
                    self.log(f'could not retry refused no-PR completion retirement: {err}')
                return None
            self.state.pop('refused_no_pr_at', None)
            if not self.dry_run:
                self._save()
        def retire_live_record():
            try:
                if conductor.retire_no_pr_done(self.hub_dir / 'state' / 'loop-done.json',
                                               expected_at=done['at'], issue=close.issue):
                    self.log('preserved live-loop no-PR completion as loop-done-refused.json')
            except (OSError, ValueError) as err:
                self.log(f'could not retire live-loop no-PR completion: {err}')
                self.state['refused_no_pr_at'] = done['at']
                if not self.dry_run:
                    self._save()
        open_pr, detail = close.open_prs(gh_call, self.branch)
        if open_pr is not False:
            self.log(f'{detail}; no-PR close waits')
            if open_pr is True:
                retire_live_record()
            return None
        closed, detail = close.issue_closed(gh_call)
        if closed is False:
            self.log(f'issue #{close.issue} is open; no-PR close waits')
            retire_live_record()
        elif closed is None:
            self.log(f'cannot check issue #{close.issue} for no-PR close: {detail}')
        return at if closed is True else None

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
            pending = self.state['close_pending']
            over = self.close_after_merge(closer.pending_number(pending))
            if pending == closer.NO_PR and over:
                return 0
        next_limit = 0.0
        next_sweep = 0.0
        next_rescue = 0.0
        next_no_pr = 0.0
        while True:
            if self.stop_file.exists():
                self.log("stop file found; exiting")
                return 0
            published = self.flush_outbox()
            if drain_deadline is None and now() >= next_limit:
                # A CLOSED unmerged PR keeps the relay watching after its notices drain.
                next_limit = now() + self.limit_interval
                texts = self.read_panes()
                self.check_limits(texts)
                try:
                    self.stall.tick(texts)
                except Exception as err:  # noqa: BLE001 - a watchdog bug must never stop the doorbell
                    self.log(f"stall watch failed: {type(err).__name__}: {err}")
            if now() >= next_sweep:
                # Watching and draining alike (#84); the autonomous close does its own helpers.
                next_sweep = now() + self.limit_interval
                try:
                    self.sweep_helpers()
                except Exception as err:  # noqa: BLE001 - a sweep bug must never stop the doorbell
                    self.log(f"helper close failed: {type(err).__name__}: {err}")
            if now() >= next_rescue:
                # Watching and draining alike (#96): a late pointer is rung while the drain runs.
                next_rescue = now() + self.limit_interval
                try:
                    self.rescue_pointers()
                except Exception as err:  # noqa: BLE001 - a rescue bug must never stop the doorbell
                    self.log(f"pointer rescue failed: {type(err).__name__}: {err}")
            self.deliver_mail()
            if drain_deadline is not None:
                pending = self.pending_terminal_mail()
                resets = {key for key, held in self.holds.items() if held.clear_pending}
                resets.update((box, '') for box in self.state.get('reset_pending', []))
                if not pending and not resets and published:
                    number, state = self.state['pr']['number'], self.state['pr'].get('state')
                    self.retire(number)
                    self.log("PR is finished; final notices delivered or read")
                    if state == 'MERGED':
                        self.close_after_merge(number)
                        return 0
                    # A CLOSED PR may be followed by an issue closed without a new PR.
                    drain_deadline = None
                    self.draining = False
                    next_pr = now() + self.pr_interval
                    continue
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
            elif published and not self.state.get('pr') and now() >= next_no_pr:
                next_no_pr = now() + self.pr_interval
                done_at = self.no_pr_close_due()
                if done_at is not None:
                    self.state['close_pending'] = closer.NO_PR
                    self.state['close_merged_at'] = done_at
                    if not self.dry_run:
                        self._save()
                        if self.close_after_merge(None):
                            return 0
                        next_no_pr = now() + self.pr_interval
                        next_pr = now()
            if drain_deadline is None and published and now() >= next_pr:
                next_pr = now() + self.pr_interval
                if self.watch_pr():
                    if self.dry_run:
                        self.log('[dry-run] PR is finished; no final mail filed')
                        return 0
                    drain_deadline = now() + TERMINAL_DRAIN_TIMEOUT
                    self.draining = True
                    self.log('PR is finished; draining final notices')
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
    parser.add_argument("--implementer-tool", choices=("codex", "claude", "kimi"), default="codex",
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
    tslog.install()     # the relay is always a pane: every line timestamped, errors too (#78)
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
