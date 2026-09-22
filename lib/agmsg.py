#!/usr/bin/env python3
# Vendored from the ai-hub tooling (MIT, same author). Kept close to the original on purpose:
# it is the tested half of this project - see tests/ there and here.
"""agmsg - the inbox for the AI agents running on this machine.

  agmsg register --role author            # tell the hub who and where you are
  agmsg agents                            # who else is running, and is their pane still open
  agmsg send --to codex-ai --subject "review the retry fix" --stdin --nudge
  agmsg list                              # your unread messages
  agmsg read                              # the oldest unread one, in full
  agmsg reply <id> --text "..." --nudge
  agmsg archive <id>

A message is a markdown file under inbox/<box>/. `--nudge` additionally types one pointer line
into the recipient's pane through peer-chat.py, which is what actually wakes an idle agent.
Long content always goes in the file; only the pointer is typed.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import hub  # noqa: E402

# A cp437 console cannot encode every character a message may carry; replace rather than crash.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(errors="replace")
        except (ValueError, OSError):
            pass

PEER_CHAT = Path(__file__).resolve().parent / "peerchat.py"


def me(create: bool = False) -> str:
    """This agent's box name. Registers on the spot when asked to."""
    entry = hub.whoami()
    if entry:
        return entry["box"]
    if create:
        return hub.register()["box"]
    fallback = hub.default_box()
    print(f"agmsg: not registered yet - using {fallback!r}. Run `agmsg register` once.",
          file=sys.stderr)
    return fallback


def body_from(args: argparse.Namespace) -> str:
    if getattr(args, "stdin", False):
        return sys.stdin.read()
    if getattr(args, "body_file", None):
        return Path(args.body_file).read_text(encoding="utf-8")
    return getattr(args, "text", None) or ""


def nudge(to: str, subject: str, message_id: str, sender: str) -> bool:
    """Type a one-line pointer into the recipient's pane. Never carries the message body."""
    pointer = (f"you have inbox mail from {sender}: {subject} "
               f"[id {message_id}] - read it with: python {Path(__file__).resolve()} read {message_id}")
    done = subprocess.run([sys.executable, str(PEER_CHAT), "--to", to, "--text", pointer],
                          capture_output=True, text=True)
    if done.returncode == 0:
        print(f"nudged {to}: {done.stdout.strip()}")
        return True
    print(f"agmsg: message is filed but the nudge failed: {done.stderr.strip()}", file=sys.stderr)
    return False


# --- commands -------------------------------------------------------------------------------

def live_panes() -> dict[str, dict]:
    """Every open pane id -> the session holding it. Raises when agwinterm is unreachable."""
    import agw
    out: dict[str, dict] = {}
    snapshot = agw.tree()
    for _, session in agw.sessions(snapshot):
        for pane in agw.panes_of(session):
            out[pane] = session
    return out


def cmd_register(args: argparse.Namespace) -> int:
    if args.pane and not args.tool:
        print("agmsg: registering a box for another pane needs --tool", file=sys.stderr)
        return 1
    entry = hub.register(args.box, tool=args.tool, role=args.role, note=args.note,
                         cwd=args.cwd, pane=args.pane)
    print(json.dumps(entry, indent=2))
    return 0


def cmd_whoami(args: argparse.Namespace) -> int:
    entry = hub.whoami()
    if not entry:
        print(f"not registered. `agmsg register` would claim the box {hub.default_box()!r}.")
        return 1
    print(json.dumps(entry, indent=2))
    return 0


def cmd_agents(args: argparse.Namespace) -> int:
    registry = hub.load_registry()["agents"]
    live: dict[str, dict] = {}
    reachable = True
    try:
        live = live_panes()
    except Exception as err:  # agwinterm not running: still list the registry
        reachable = False
        print(f"(agwinterm not reachable: {err})", file=sys.stderr)
    if args.json:
        # pane_open is null, not false, when the terminal could not be asked. An unreachable
        # terminal means unknown, not gone - the same rule `sweep` obeys - and a machine consumer
        # has to be able to tell the two apart without parsing stderr.
        for entry in registry.values():
            entry["pane_open"] = (entry.get("pane") in live) if reachable else None
        print(json.dumps({"terminal_reachable": reachable, "agents": registry},
                         indent=2, sort_keys=True))
        return 0
    if not registry:
        print("no agents registered yet - each one should run `agmsg register`.")
        return 0
    print(f"{'BOX':22} {'TOOL':7} {'ROLE':17} {'PANE':8} {'STATUS':10} {'UNREAD':>6}  SEEN  CWD")
    for box in sorted(registry):
        entry = registry[box]
        session = live.get(entry.get("pane") or "")
        if session:
            state = session.get("status", "-")
        elif not reachable:
            state = "unknown"      # the terminal was never asked; do not report a pane as gone
        elif entry.get("closed_at"):
            state = f"closed {hub.age(entry['closed_at'])}"
        else:
            state = "closed"
        role = (entry.get("role") or "-")
        role = role[:16] + "…" if len(role) > 17 else role
        print(f"{box:22} {entry.get('tool','?'):7} {role:17} "
              f"{(entry.get('pane') or '-')[:8]:8} {state:10} {len(hub.unread(box)):>6}  "
              f"{hub.age(entry.get('last_seen')):>4}  {entry.get('cwd','')}")
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    sender = me(create=True)
    hub.touch(sender)
    body = body_from(args).strip()
    if not body:
        print("agmsg: empty message body", file=sys.stderr)
        return 1
    targets = [b for b in hub.boxes() if b != sender] if args.to == "all" else [args.to]
    if not targets:
        print("agmsg: no recipients", file=sys.stderr)
        return 1
    failures = 0
    for target in targets:
        if args.to != "all" and not hub.lookup(target):
            print(f"agmsg: {target!r} is not a registered box. `agmsg agents` lists them; "
                  f"pass --force to file it anyway.", file=sys.stderr)
            if not args.force:
                return 1
        path = hub.write_message(to=target, sender=sender, subject=args.subject, body=body,
                                 kind=args.kind, thread=args.thread, refs=args.ref)
        message = hub.parse_message(path)
        print(f"filed {message['id']} -> {target}  ({path})")
        if args.nudge and not nudge(target, args.subject, message["id"], sender):
            failures += 1
    return 1 if failures else 0


def cmd_list(args: argparse.Namespace) -> int:
    box = args.box or me()
    hub.ensure_box(box)
    paths = hub.unread(box)
    if args.all:
        paths += sorted((hub.box_dir(box) / "read").glob("*.md"))
    if not paths:
        print(f"{box}: no unread mail")
        return 0
    for path in paths:
        message = hub.parse_message(path)
        where = "read" if path.parent.name == "read" else "NEW "
        print(f"{where} {message['id']}  [{message.get('kind','message')}] "
              f"from {message.get('from','?')}: {message.get('subject','')}")
    return 0


def cmd_read(args: argparse.Namespace) -> int:
    box = args.box or me()
    hub.ensure_box(box)
    if args.id:
        path = hub.find_message(box, args.id)
        if not path:
            print(f"agmsg: no message {args.id!r} in {box}", file=sys.stderr)
            return 1
    else:
        pending = hub.unread(box)
        if not pending:
            print(f"{box}: no unread mail")
            return 0
        path = pending[0]
    message = hub.parse_message(path)
    print(f"--- {message['id']}")
    print(f"from:    {message.get('from','?')}")
    print(f"to:      {message.get('to', box)}")
    print(f"kind:    {message.get('kind','message')}")
    print(f"subject: {message.get('subject','')}")
    if message.get("thread"):
        print(f"thread:  {message['thread']}")
    for ref in message.get("refs", []):
        print(f"ref:     {ref}")
    print()
    print(message["body"])
    if not args.keep and path.parent.name not in ("read", "archive"):
        hub.mark_read(path)
    hub.append_log({"at": hub.now_iso(), "event": "read", "id": message["id"], "box": box})
    return 0


def cmd_reply(args: argparse.Namespace) -> int:
    box = me(create=True)
    path = hub.find_message(box, args.id)
    if not path:
        print(f"agmsg: no message {args.id!r} in {box}", file=sys.stderr)
        return 1
    original = hub.parse_message(path)
    args.to = original.get("from", "")
    args.subject = args.subject or f"re: {original.get('subject','')}"
    args.thread = original.get("thread") or original["id"]
    args.kind = args.kind or ("answer" if original.get("kind") == "question" else "message")
    args.ref = args.ref or []
    args.force = False
    return cmd_send(args)


def cmd_archive(args: argparse.Namespace) -> int:
    box = args.box or me()
    path = hub.find_message(box, args.id)
    if not path:
        print(f"agmsg: no message {args.id!r} in {box}", file=sys.stderr)
        return 1
    target = hub.box_dir(box) / "archive" / path.name
    target.parent.mkdir(parents=True, exist_ok=True)
    path.replace(target)
    print(f"archived {path.name}")
    return 0


def cmd_nudge(args: argparse.Namespace) -> int:
    sender = me(create=True)
    text = args.text or f"ping from {sender}"
    done = subprocess.run([sys.executable, str(PEER_CHAT), "--to", args.to, "--text", text],
                          capture_output=True, text=True)
    sys.stdout.write(done.stdout)
    sys.stderr.write(done.stderr)
    return done.returncode


def cmd_sweep(args: argparse.Namespace) -> int:
    """Reconcile the registry against the open panes. An unreachable terminal means unknown,
    not gone, so a failed tree() writes nothing."""
    try:
        live = live_panes()
    except Exception as err:
        print(f"agmsg: agwinterm is unreachable ({err}) - a terminal that is not running is not "
              f"evidence that a pane is gone, so nothing was changed.", file=sys.stderr)
        return 1
    if not live:
        print("agmsg: no open panes reported - refusing to sweep the whole registry.",
              file=sys.stderr)
        return 1
    report = hub.sweep(live, prune=args.prune, dry_run=args.dry_run)
    verb = "would " if args.dry_run else ""
    for box in report["pruned"]:
        print(f"{verb}remove  {box}  (pane gone)")
    for box in report["marked"]:
        print(f"{verb}mark    {box}  (pane gone)")
    for box in report["revived"]:
        print(f"{verb}revive  {box}  (pane is open again)")
    for box in report["already_closed"]:
        print(f"keep    {box}  (already marked closed)")
    for box in report["no_pane"]:
        print(f"keep    {box}  (no pane recorded)")
    for box in report["live"]:
        print(f"keep    {box}  (pane open)")
    touched = len(report["pruned"]) + len(report["marked"]) + len(report["revived"])
    if not touched:
        print("registry is already in step with the open panes.")
    return 0


def cmd_peek(args: argparse.Namespace) -> int:
    """Read what another agent's pane is showing. Reads only - it types nothing and changes
    nothing, so it is always safe, including while that agent is mid-turn or on a dialog."""
    import agw
    pane = args.pane
    if not pane:
        entry = hub.lookup(args.box) if args.box else None
        if entry:
            pane = entry.get("pane")
        elif args.box:
            print(f"agmsg: {args.box!r} is not a registered box", file=sys.stderr)
            return 1
        else:  # no target: the other pane of my own split
            mine = agw.my_pane()
            located = agw.find_pane(mine, agw.tree()) if mine else None
            if not located:
                print("agmsg: no pane to peek at - pass --box or --pane", file=sys.stderr)
                return 1
            others = [x for x in agw.panes_of(located[1]) if x != mine]
            if len(others) != 1:
                print(f"agmsg: {len(others)} peer panes here - pass --pane", file=sys.stderr)
                return 1
            pane = others[0]
    text = agw.pane_text(pane)
    lines = text.splitlines()
    if args.lines > 0:
        lines = lines[-args.lines:]
    print(chr(10).join(lines))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    print(f"hub:        {hub.HUB}")
    print(f"tool:       {hub.detect_tool()}")
    print(f"box:        {(hub.whoami() or {}).get('box') or '(unregistered, would be ' + hub.default_box() + ')'}")
    try:
        import agw
        print(f"agwinterm:  {agw.version()}")
        pane = agw.my_pane()
        print(f"my pane:    {pane}")
        located = agw.find_pane(pane, agw.tree()) if pane else None
        if located:
            _, session, index = located
            panes = agw.panes_of(session)
            print(f"session:    {session['name']} ({len(panes)} pane(s), I am #{index})")
            if len(panes) > 1:
                print(f"peer pane:  {[p for p in panes if p != pane][0]}")
            else:
                print("peer pane:  none - no split, so there is nobody to chat with yet")
    except Exception as err:
        print(f"agwinterm:  unreachable ({err})")
    registry = hub.load_registry()["agents"]
    print(f"registered: {', '.join(sorted(registry)) or '(none)'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agmsg", description="inbox for the agents on this machine")
    subs = parser.add_subparsers(dest="command", required=True)

    p = subs.add_parser("register", help="claim a box for this agent")
    p.add_argument("box", nargs="?")
    p.add_argument("--tool", choices=hub.TOOLS)
    p.add_argument("--role", help="author, reviewer, planner ...")
    p.add_argument("--note", help="one line about what this agent is doing")
    p.add_argument("--pane", help="claim this box FOR ANOTHER agent, on that pane id "
                                  "(for an agent that started before the hub existed); needs --tool")
    p.add_argument("--cwd", help="that agent's working directory")
    p.set_defaults(func=cmd_register)

    p = subs.add_parser("whoami", help="show this agent's registry entry")
    p.set_defaults(func=cmd_whoami)

    p = subs.add_parser("agents", help="list registered agents and whether their pane is open")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_agents)

    p = subs.add_parser("send", help="file a message in another agent's inbox")
    p.add_argument("--to", required=True, help="box name, or 'all'")
    p.add_argument("--subject", required=True)
    p.add_argument("--kind", default="message", choices=hub.KINDS)
    p.add_argument("--thread")
    p.add_argument("--ref", action="append", default=[], help="a path the message is about")
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--stdin", action="store_true")
    source.add_argument("--text")
    source.add_argument("--body-file")
    p.add_argument("--nudge", action="store_true", help="also type a pointer into their pane")
    p.add_argument("--force", action="store_true", help="file for an unregistered box anyway")
    p.set_defaults(func=cmd_send)

    p = subs.add_parser("list", help="list mail")
    p.add_argument("--box")
    p.add_argument("--all", action="store_true", help="include already-read mail")
    p.set_defaults(func=cmd_list)

    p = subs.add_parser("read", help="read a message (marks it read)")
    p.add_argument("id", nargs="?", help="message id or a unique fragment; default: oldest unread")
    p.add_argument("--box")
    p.add_argument("--keep", action="store_true", help="leave it unread")
    p.set_defaults(func=cmd_read)

    p = subs.add_parser("reply", help="reply to a message you received")
    p.add_argument("id")
    p.add_argument("--subject")
    p.add_argument("--kind", choices=hub.KINDS)
    p.add_argument("--ref", action="append", default=[])
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--stdin", action="store_true")
    source.add_argument("--text")
    source.add_argument("--body-file")
    p.add_argument("--nudge", action="store_true")
    p.set_defaults(func=cmd_reply)

    p = subs.add_parser("archive", help="move a message out of the way")
    p.add_argument("id")
    p.add_argument("--box")
    p.set_defaults(func=cmd_archive)

    p = subs.add_parser("nudge", help="type one line into another agent's pane, with no mail")
    p.add_argument("--to", required=True)
    p.add_argument("--text")
    p.set_defaults(func=cmd_nudge)

    p = subs.add_parser("sweep", help="mark or remove entries whose pane is gone")
    p.add_argument("--prune", action="store_true", help="delete the dead entries instead of "
                                                        "stamping them closed_at")
    p.add_argument("--dry-run", action="store_true", help="report only; write nothing")
    p.set_defaults(func=cmd_sweep)

    p = subs.add_parser("peek", help="read another agent's pane (read-only; types nothing)")
    p.add_argument("--box", help="a registered box")
    p.add_argument("--pane", help="an explicit pane id")
    p.add_argument("--lines", type=int, default=40, help="how many trailing rows (0 = all)")
    p.set_defaults(func=cmd_peek)

    p = subs.add_parser("doctor", help="what this agent can see: pane, peer, registry")
    p.set_defaults(func=cmd_doctor)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except (ValueError, OSError) as err:
        print(f"agmsg: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
