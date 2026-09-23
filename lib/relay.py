#!/usr/bin/env python3
"""relay - the workbench's doorbell and its eye on GitHub.

Two jobs, one loop, one process per issue, running in its own visible agwinterm session:

1. **Mail.** Agents never type into each other's panes. They write a message file into the
   workbench mailbox (`agmsg send`, no `--nudge`), and the relay types a one-line pointer into the
   recipient's pane. This is what lets Codex stay sandboxed: its sandbox denies the agwinterm control
   pipe, so it cannot ring a doorbell itself - but it can write a file inside its own workspace.

2. **The pull request.** Once a PR exists for the issue branch, the relay watches it and files a
   message to Claude on every event that needs acting on: a new review, a changed review decision,
   a new comment, a merge, a close. The loop ends when the PR is merged (or closed).

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
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from peerchat import is_busy  # noqa: E402 - also available as relay.is_busy

PR_FIELDS = "number,url,state,reviewDecision,mergedAt,reviews,comments,headRefName"
HOLD_ALERT_AFTER = 60.0
ALERT_EVERY = 300.0


@dataclass(frozen=True)
class Peer:
    box: str       # mailbox name: claude | codex
    tool: str      # peerchat profile: claude | codex
    pane: str      # agwinterm pane id


# --- pure logic (tested without a terminal or a network) ------------------------------------

def now() -> float:
    return time.monotonic()


@dataclass
class Hold:
    first_at: float
    reason: str
    last_alert_at: float | None = None
    alerted: bool = False


def pointer_text(message: dict[str, Any], agmsg: Path, hub_dir: Path) -> str:
    """The single line typed into a pane. Never the body - that stays in the file."""
    sender = message.get("from", "?")
    subject = message.get("subject", "")
    mid = message.get("id", "")
    return (f"workbench mail from {sender}: {subject} [id {mid}] - read it with: "
            f"python {agmsg} read {mid}  (AI_HUB={hub_dir})")


def pr_events(old: dict[str, Any] | None, new: dict[str, Any] | None) -> list[dict[str, str]]:
    """What changed on the PR between two snapshots, as messages worth filing to Claude.

    A snapshot is the `gh pr view --json` object plus `inline`, the list of line comments. Only
    changes produce events: the relay can restart and re-read the same PR without re-announcing it.
    """
    events: list[dict[str, str]] = []
    if new is None:
        return events
    number = new.get("number")
    if old is None:
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
                           "body": f"Merged at {new.get('mergedAt')}. {new.get('url')}"})
        elif state == "CLOSED":
            events.append({"kind": "note", "subject": f"PR #{number} was CLOSED without merging",
                           "body": new.get("url", "")})
    return events


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


def finished(snapshot: dict[str, Any] | None) -> bool:
    return bool(snapshot) and snapshot.get("state") in ("MERGED", "CLOSED")


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
        self.agmsg = HERE / "agmsg.py"
        self.holds: dict[tuple[str, str], Hold] = {}

    def _load(self) -> dict[str, Any]:
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"announced": [], "pr": None}

    def _save(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_file)

    def log(self, text: str) -> None:
        print(f"{time.strftime('%H:%M:%S')} {text}", flush=True)

    # mail -----------------------------------------------------------------------------------
    def hold(self, peer: Peer, mid: str, reason: str, *, failed: bool = False) -> None:
        instant = now()
        entry = self.holds.setdefault((peer.box, mid), Hold(instant, reason))
        entry.reason = reason
        if failed:
            self.log(f"FAILED ringing {peer.box} for {mid}: {reason}")
        else:
            self.log(f"{peer.box} not ready ({reason}); holding {mid}")
        if (failed or instant - entry.first_at >= HOLD_ALERT_AFTER) and (
                entry.last_alert_at is None or instant - entry.last_alert_at >= ALERT_EVERY):
            if not self.dry_run:
                entry.last_alert_at = instant
                entry.alerted = True
                self.alert(peer, mid, reason)

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
        entry = self.holds.pop((peer.box, mid), None)
        if entry and entry.alerted and not self.dry_run and not any(
                box == peer.box and held.alerted for (box, _), held in self.holds.items()):
            try:
                agw.set_status('idle', pane_id=peer.pane)
            except (agw.CtlError, OSError) as err:
                self.log(f"could not clear relay status for {peer.box}: {err}")
        return entry

    def deliver_mail(self) -> None:
        import agw
        import peerchat
        announced = set(self.state.get("announced", []))
        for peer in self.peers:
            messages = [(path, self.hub.parse_message(path)) for path in self.hub.unread(peer.box)]
            unread_ids = {message.get('id', path.stem) for path, message in messages}
            for box, mid in list(self.holds):
                if box == peer.box and (mid not in unread_ids or mid in announced):
                    self.clear(peer, mid)
            for path, message in messages:
                mid = message.get('id', path.stem)
                if mid in announced:
                    continue
                try:
                    if peer.tool == "claude" and is_busy(agw.pane_text(peer.pane)):
                        raise peerchat.Refused('mid-turn; waiting for the agent to finish')
                    text = peerchat.compose_text("Chat from Workbench: ",
                                                 pointer_text(message, self.agmsg, self.hub_dir))
                    if self.dry_run:
                        self.log(f"[dry-run] would ring {peer.box}: {text}")
                    else:
                        outcome = peerchat.send(peer.pane, peerchat.PROFILES[peer.tool], text,
                                                dry_run=False, retry=False)
                        held = self.clear(peer, mid)
                        duration = f" after holding {now() - held.first_at:.0f}s" if held else ''
                        self.log(f"rang {peer.box} for {mid} ({message.get('subject', '')}) [{outcome}]{duration}")
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
    def fetch_pr(self) -> dict[str, Any] | None:
        done = subprocess.run(["gh", "pr", "view", self.branch, "--repo", self.repo, "--json", PR_FIELDS],
                              capture_output=True, text=True, encoding="utf-8", errors="replace")
        if done.returncode != 0:
            return None                    # no PR for this branch yet, or gh is unhappy: try later
        snapshot = json.loads(done.stdout)
        inline = subprocess.run(["gh", "api", f"repos/{self.repo}/pulls/{snapshot['number']}/comments",
                                 "--paginate"], capture_output=True, text=True, encoding="utf-8",
                                errors="replace")
        snapshot["inline"] = json.loads(inline.stdout) if inline.returncode == 0 and inline.stdout.strip() else []
        return snapshot

    def watch_pr(self) -> bool:
        """Returns True when the loop is over."""
        snapshot = self.fetch_pr()
        if snapshot is None:
            return False
        for event in pr_events(self.state.get("pr"), snapshot):
            recipients = ["claude"]
            if "MERGED" in event["subject"] or "CLOSED" in event["subject"]:
                recipients = [p.box for p in self.peers]
            for box in recipients:
                self.hub.write_message(to=box, sender="github", subject=event["subject"],
                                       body=event["body"], kind=event["kind"])
            self.log(f"github: {event['subject']}")
        self.state["pr"] = snapshot
        self._save()
        return finished(snapshot)

    def run(self) -> int:
        self.log(f"relay up: {self.repo} {self.branch}; mailbox {self.hub_dir}")
        next_pr = 0.0
        while True:
            if self.stop_file.exists():
                self.log("stop file found; exiting")
                return 0
            self.deliver_mail()
            if time.time() >= next_pr:
                next_pr = time.time() + self.pr_interval
                if self.watch_pr():
                    self.deliver_mail()          # announce the merge before leaving
                    self.log("PR is finished; the relay's job is done")
                    return 0
            time.sleep(self.mail_interval)


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
