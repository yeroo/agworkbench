#!/usr/bin/env python3
"""wb - open the workbench's visible helper sessions without composing terminal commands by hand.

  wb.py revmux --round 1 --scope .workbench/review/scope-r1.md    # review round, own session
  wb.py human-review --base origin/main                          # revdiff, selected, for the human
  wb.py status blocked --sound                                    # this pane's sidebar status
  wb.py wait-mail                                                 # background inbox waiter

Why a helper: Claude's shell is Git Bash, where $PWD is a POSIX path (/c/Users/...) that PowerShell
cannot use, and quoting a PowerShell command inside a bash string inside an agwintermctl argument
is three quoting languages deep. Everything here is derived from AI_HUB, which the workbench set.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import agw  # noqa: E402
import hub  # noqa: E402


def checkout() -> Path:
    hub = os.environ.get("AI_HUB")
    if not hub:
        raise SystemExit("wb: AI_HUB is not set - run this inside an agworkbench pane")
    return Path(hub).resolve().parent


def issue_number(root: Path) -> str:
    branch = subprocess.run(["git", "-C", str(root), "branch", "--show-current"],
                            capture_output=True, text=True).stdout.strip()
    match = re.match(r"issue-(\d+)", branch)
    return match.group(1) if match else "?"


def ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def pane_command(script: str, **params: str) -> str:
    shell = "pwsh" if shutil.which("pwsh") else "powershell.exe"
    parts = [shell, "-NoLogo", "-ExecutionPolicy", "Bypass", "-File", ps_quote(str(HERE / script))]
    for key, value in params.items():
        parts += [f"-{key}", ps_quote(value)]
    return " ".join(parts)


def open_session(name: str, cwd: Path, command: str, select: bool) -> str:
    args = {"name": name, "cwd": str(cwd), "command": command}
    pane = agw.my_pane()
    found = agw.find_pane(pane, agw.tree()) if pane else None
    workspace = found[0].get('id') if found else None
    if workspace:
        args['workspace'] = workspace
    else:
        print('wb: caller workspace not found; opening helper in the active workspace', file=sys.stderr)
    if not select:
        args["no-select"] = True
    result = agw.request("session.new", args=args)
    return str(result).split()[0] if result else ""


def revmux_profile(root: Path) -> str:
    """The profile the launcher resolved for this checkout (#20): claude-only when Claude is the
    implementer and Codex may be out of quota, comprehensive otherwise, or the human's revmuxProfile."""
    try:
        saved = json.loads((root / ".workbench" / "state" / "implementer.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return "comprehensive"
    profile = saved.get("revmuxProfile") if isinstance(saved, dict) else None
    return profile if isinstance(profile, str) and re.fullmatch(r"[A-Za-z0-9._-]+", profile) else "comprehensive"


def cmd_revmux(args: argparse.Namespace) -> int:
    root = checkout()
    scope = (root / args.scope).resolve() if not Path(args.scope).is_absolute() else Path(args.scope)
    if not scope.is_file():
        raise SystemExit(f"wb: scope file not found: {scope}")
    command = pane_command("run-revmux.ps1", Checkout=str(root), ScopeFile=str(scope),
                           Round=str(args.round), Profile=args.profile or revmux_profile(root))
    sid = open_session(f"#{issue_number(root)} revmux r{args.round}", root, command, select=False)
    print(f"revmux round {args.round} running in session {sid}; the report will arrive as mail")
    return 0


def cmd_human_review(args: argparse.Namespace) -> int:
    root = checkout()
    command = pane_command("human-review.ps1", Checkout=str(root), Base=args.base)
    sid = open_session(f"#{issue_number(root)} your review", root, command, select=True)
    print(f"human review open in session {sid}; annotations will arrive as mail from 'human'")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    agw.set_status(args.state, sound=args.sound, blink=args.sound)
    return 0


def cmd_loop_state(args: argparse.Namespace) -> int:
    # Reports stay in this checkout; the conductor alone owns the global queue.
    from conductor import write_loop_state
    try:
        write_loop_state(checkout(), args.state, args.pr, args.reason)
        return 0
    except (OSError, ValueError, KeyError, TypeError) as err:
        print(f'wb: loop-state: {err}', file=sys.stderr)
        return 2


def now() -> float:
    return time.monotonic()


def pause(seconds: float) -> None:
    time.sleep(seconds)


def cmd_wait_mail(args: argparse.Namespace) -> int:
    """Wake on unread mail, including replies arriving before this process starts. Never consume it."""
    try:
        if not math.isfinite(args.interval) or not 0 < args.interval <= 3600:
            raise ValueError('--interval must be finite, greater than zero and at most 3600 seconds')
        if not math.isfinite(args.timeout) or not 0 <= args.timeout <= 1440:
            raise ValueError('--timeout must be finite and between 0 and 1440 minutes')
        root = os.environ.get('AI_HUB')
        if not root or not Path(root).expanduser().is_dir():
            raise ValueError('AI_HUB must name an existing mailbox directory')
        hub.reload_paths()
        box = args.box
        if box is None:
            box = os.environ.get('AI_BOX') or (hub.whoami() or {}).get('box') or 'claude'
        hub.box_dir(box)  # Validate without creating a missing inbox.
        started = now()
        while True:
            messages = []
            for path in hub.unread(box):
                try:
                    messages.append(hub.parse_message(path))
                except FileNotFoundError:
                    continue  # Another reader moved it after our listing.
            if messages:
                for message in messages:
                    print(f"NEW MAIL: {message['id']} {message.get('from', '?')} {message['subject']}")
                return 0
            remaining = args.timeout - (now() - started) / 60
            if remaining <= 0:
                print(f'no new mail in {args.timeout:g} minutes')
                return 3
            pause(min(args.interval, remaining * 60))
    except (OSError, ValueError) as err:
        print(f'wb: wait-mail: {err}', file=sys.stderr)
        return 2


def main() -> int:
    # Redirected Windows streams may use cp1252/cp437; preserve IDs even when prose cannot encode.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            try:
                stream.reconfigure(errors='replace')
            except (ValueError, OSError):
                pass
    parser = argparse.ArgumentParser(prog="wb")
    subs = parser.add_subparsers(dest="command", required=True)
    p = subs.add_parser('loop-state', help='report a queue member phase without terminal access')
    p.add_argument('state', choices=['pr-open', 'blocked', 'resumed'])
    p.add_argument('--pr')
    p.add_argument('--reason')
    p.set_defaults(func=cmd_loop_state)
    p = subs.add_parser("revmux", help="run a revmux round in its own visible session")
    p.add_argument("--round", type=int, required=True)
    p.add_argument("--scope", required=True, help="scope file, relative to the clone or absolute")
    p.add_argument("--profile", help="revmux profile (default: the one the launcher saved for this checkout)")
    p.set_defaults(func=cmd_revmux)
    p = subs.add_parser("human-review", help="open revdiff for the human, selected")
    p.add_argument("--base", required=True, help="e.g. origin/main")
    p.set_defaults(func=cmd_human_review)
    p = subs.add_parser("status", help="set this pane's sidebar status")
    p.add_argument("state", choices=["idle", "active", "blocked", "completed"])
    p.add_argument("--sound", action="store_true")
    p.set_defaults(func=cmd_status)
    p = subs.add_parser('wait-mail', help='wait for unread mail without using the terminal')
    p.add_argument('--box', help='mailbox (default: AI_BOX, pane registry entry, or claude)')
    p.add_argument('--timeout', type=float, default=55, metavar='MIN', help='timeout, 0..1440 minutes (default: 55)')
    p.add_argument('--interval', type=float, default=10, metavar='SEC', help='poll interval, >0..3600 seconds (default: 10)')
    p.set_defaults(func=cmd_wait_mail)
    args = parser.parse_args()
    try:
        return args.func(args)
    except agw.CtlError as err:
        print(f"wb: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
