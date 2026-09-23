#!/usr/bin/env python3
"""wb - open the workbench's visible helper sessions without composing terminal commands by hand.

  wb.py revmux --round 1 --scope .workbench/review/scope-r1.md    # review round, own session
  wb.py human-review --base origin/main                          # revdiff, selected, for the human
  wb.py status blocked --sound                                    # this pane's sidebar status

Why a helper: Claude's shell is Git Bash, where $PWD is a POSIX path (/c/Users/...) that PowerShell
cannot use, and quoting a PowerShell command inside a bash string inside an agwintermctl argument
is three quoting languages deep. Everything here is derived from AI_HUB, which the workbench set.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import agw  # noqa: E402


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


def cmd_revmux(args: argparse.Namespace) -> int:
    root = checkout()
    scope = (root / args.scope).resolve() if not Path(args.scope).is_absolute() else Path(args.scope)
    if not scope.is_file():
        raise SystemExit(f"wb: scope file not found: {scope}")
    command = pane_command("run-revmux.ps1", Checkout=str(root), ScopeFile=str(scope),
                           Round=str(args.round), Profile=args.profile)
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


def main() -> int:
    parser = argparse.ArgumentParser(prog="wb")
    subs = parser.add_subparsers(dest="command", required=True)
    p = subs.add_parser("revmux", help="run a revmux round in its own visible session")
    p.add_argument("--round", type=int, required=True)
    p.add_argument("--scope", required=True, help="scope file, relative to the clone or absolute")
    p.add_argument("--profile", default="comprehensive")
    p.set_defaults(func=cmd_revmux)
    p = subs.add_parser("human-review", help="open revdiff for the human, selected")
    p.add_argument("--base", required=True, help="e.g. origin/main")
    p.set_defaults(func=cmd_human_review)
    p = subs.add_parser("status", help="set this pane's sidebar status")
    p.add_argument("state", choices=["idle", "active", "blocked", "completed"])
    p.add_argument("--sound", action="store_true")
    p.set_defaults(func=cmd_status)
    args = parser.parse_args()
    try:
        return args.func(args)
    except agw.CtlError as err:
        print(f"wb: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
