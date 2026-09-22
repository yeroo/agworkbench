#!/usr/bin/env python3
# Vendored from the ai-hub tooling (MIT, same author). Kept close to the original on purpose:
# it is the tested half of this project - see tests/ there and here.
"""peer-chat - type one line into the other agent's composer, in this agwinterm window.

A Windows/agwinterm port of umputun's agterm cookbook recipe `two-agent-chat`
(https://github.com/umputun/agterm/tree/master/cookbook/two-agent-chat). Same idea and the same
refusals; the differences are forced by the terminal:

  * agterm exposes `surface cursor`, so the original proves an empty composer by reading the caret
    column. agwinterm 0.17.x has no cursor read, so this port proves it from the rendered composer
    line instead, against a whitelist of each agent's empty-box placeholder. Anything it does not
    recognise is a refusal, never a send.
  * agterm reports the foreground command per pane, so the original checks that the target really
    runs `codex`. agwinterm's tree does not carry that, so the target's tool comes from the hub
    registry (`agmsg register`), which the agent itself writes, plus the composer shape - a Codex
    composer cannot be mistaken for a Claude one.
  * panes are addressed by pane id from `tree --json`, not by `--pane left|right`.

What survives unchanged is the important half: the message is TYPED into a live TUI, so every
check fails closed, a refusal before typing is retried and a failure after typing never is.

Usage:
  peer-chat.py --to codex --stdin < message.txt
  peer-chat.py --to claude-ai --text "one paragraph, no newlines"
  peer-chat.py --to codex --text "..." --dry-run     # run every check, type nothing

Exit codes: 0 sent, 1 refused or failed, 130 interrupted.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import agw  # noqa: E402
import hub  # noqa: E402

# A cp437 console cannot encode every character a message may carry; replace rather than crash.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(errors="replace")
        except (ValueError, OSError):
            pass

BOX_LINES = 40           # how much of the pane bottom is read as the composer region
RETRY_ATTEMPTS = 5       # pre-write refusals only
RETRY_DELAY = 10.0
SETTLE = 0.35            # let the TUI redraw before reading it back
VERIFY_TIMEOUT = 3.0
MAX_TYPED = 1200         # longer than this belongs in the inbox, not in a composer
FRAGMENT = 24            # how much of the typed text must be visible before submitting

# Claude Code draws its composer as a `>` line between two horizontal rules; agwinterm renders the
# rule with box-drawing dashes and the prompt glyph as `>` (macOS/agterm shows `>`).
RULE_RE = re.compile(r"^\s*[─━—-]{10,}\s*$")
CLAUDE_PROMPT_RE = re.compile(r"^\s*[>❯]\s?(.*?)\s*$")
# Codex draws `›`, and `»` when reasoning effort is set to ultra. `!` is shell mode and is
# deliberately not matched, so nothing is ever typed there.
CODEX_PROMPT_RE = re.compile(r"^[›»][\s ]*(.*?)\s*$")
CODEX_SHELL_PROMPT_RE = re.compile(r"^![\s ]*(.*?)\s*$")
# A composed line longer than the pane is wrapped onto two-space-indented continuation rows, which
# is also how the footer and an overlay row are drawn - the difference is only that a continuation
# follows the prompt row inside the same block.
CODEX_CONT_RE = re.compile(r"^ {2}\s*(\S.*?)\s*$")
# Codex indents its footer rows by two spaces. Only the final row is stripped: a multi-row overlay
# and an indented modal choice have the same shape, so the guard fails closed on both.
FOOTER_RE = re.compile(r"^ {2}\S")
# A highlighted chooser row - a permission prompt, a trust dialog, a picker.
CHOOSER_RE = re.compile(r"^\s*[>❯›]\s+\d+\.\s+\S")

# Placeholder text an EMPTY composer draws. Anything else in the composer is treated as a draft.
#
# These are matched with fullmatch, against the WHOLE joined content. Prefix-matching them was a
# hole: `codex_composer` joins the prompt row with the indented rows under it, so a modal choice
# list drawn below an empty box came back as "Ask Codex to do anything 1. Yes 2. No" and a
# prefix match still called that empty. Whatever the box holds beyond the placeholder, the answer
# is "not empty" - refusing is the safe direction.
#
# The Claude patterns admit prose only, with no digits, for the same reason: a joined "1. Yes"
# must break the match. An unrecognised placeholder therefore refuses rather than types, which is
# a nuisance to fix (add the pattern) and never a message in the wrong place.
CLAUDE_HINTS = (
    re.compile(r"\s*"),
    re.compile(r'Try\s+"[^"]*"\s*'),
    re.compile(r"Ask Claude[A-Za-z .,'…-]*"),
)
CODEX_HINTS = (
    re.compile(r"Ask Codex to do anything\s*"),
    re.compile(r"\s*"),
)


@dataclass(frozen=True)
class Profile:
    tool: str
    display: str
    submit: str      # the key that sends the composed line
    hints: tuple


PROFILES = {
    # Codex takes Tab: mid-turn it queues the line as a follow-up, on an idle composer it submits.
    # Return would steer a running turn instead.
    "codex": Profile("codex", "Codex", "\t", CODEX_HINTS),
    # Claude Code has no queue-only key, so it takes Return - which lands in whatever turn is
    # running. Send to Claude when you have finished a thought, not in the middle of one.
    "claude": Profile("claude", "Claude", "\n", CLAUDE_HINTS),
}


class Refused(RuntimeError):
    """A pre-write check failed. Nothing was typed, so retrying is safe."""


class Failed(RuntimeError):
    """Something failed after text was typed. Never retried."""


# --- reading the composer -------------------------------------------------------------------

def trailing_block(text: str) -> list[str]:
    """The last non-blank block of the pane, with one trailing footer row stripped."""
    lines = text.splitlines()[-BOX_LINES:]
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and FOOTER_RE.match(lines[-1]):
        lines.pop()
        while lines and not lines[-1].strip():
            lines.pop()
    start = len(lines)
    while start and lines[start - 1].strip():
        start -= 1
    return lines[start:]


def claude_composer(text: str) -> str | None:
    """Claude's composer content, read from between the last two rules. None when not visible."""
    lines = text.splitlines()[-BOX_LINES:]
    rules = [i for i, line in enumerate(lines) if RULE_RE.match(line)]
    if len(rules) < 2:
        return None
    body = lines[rules[-2] + 1:rules[-1]]
    if not body:
        return None
    first = CLAUDE_PROMPT_RE.match(body[0])
    if not first:
        return None
    rows = [first.group(1)] + [line.strip() for line in body[1:]]
    return " ".join(row for row in rows if row).strip()


def codex_composer(text: str) -> str | None:
    """Codex's composer content, prompt row plus its wrapped rows. None when not visible.

    A long line wraps onto two-space-indented continuation rows, so reading the prompt row alone
    would hand `verify_typed` the first row of a long message and nothing else. The rows are joined
    here the way `claude_composer` joins the rows between the rules. Shell mode (`!`) resets the
    content, so a shell line is never a target, and any row the parser does not recognise resets it
    too: an unread frame must refuse, not send.
    """
    rows: list[str] | None = None
    for line in trailing_block(text):
        if CODEX_SHELL_PROMPT_RE.match(line):
            rows = None
            continue
        match = CODEX_PROMPT_RE.match(line)
        if match:
            rows = [match.group(1)]
            continue
        cont = CODEX_CONT_RE.match(line)
        if cont is not None and rows is not None:
            rows.append(cont.group(1))
            continue
        rows = None
    if rows is None:
        return None
    return " ".join(row for row in rows if row).strip()


def composer(profile: Profile, text: str) -> str | None:
    return claude_composer(text) if profile.tool == "claude" else codex_composer(text)


def looks_empty(profile: Profile, content: str) -> bool:
    """True only when the composer holds nothing but a placeholder. fullmatch, never match: a
    prefix match calls "placeholder + a modal choice list" empty, and then peer-chat types."""
    return any(hint.fullmatch(content) for hint in profile.hints)


def dialog_visible(text: str) -> bool:
    lines = text.splitlines()[-25:]
    return any(CHOOSER_RE.match(line) for line in lines)


# Control characters that survive whitespace folding. NUL is the dangerous one: agwinterm's
# session.type writes what it is given, and agterm fixed a bug in v0.25.0 where a NUL truncated the
# injected text while the Return that followed it still fired - typing half a message and
# submitting it. ESC and the rest can move a TUI's state without printing anything.
# normalize() folds space, tab, CR and LF; these are what is left.
CONTROL_CHARS = frozenset(
    [chr(c) for c in range(0x00, 0x20) if c not in (0x09, 0x0a, 0x0b, 0x0c, 0x0d)]
    + [chr(0x7f)]
    + [chr(c) for c in range(0x80, 0xa0)]
)


def reject_control_bytes(text: str) -> None:
    """Refuse anything that would reach another agent's TUI as a control code rather than as text.
    Pre-write, so it is a Refused: nothing has been typed when this runs."""
    found = sorted({ord(ch) for ch in text if ch in CONTROL_CHARS})
    if found:
        names = ", ".join(f"U+{code:04X}" for code in found[:5])
        raise Refused(f"the message carries control characters ({names}); refusing to type it")


def normalize(message: str) -> str:
    """One paragraph. A newline in a composer submits the fragment before it."""
    return " ".join(message.split())


# --- targeting ------------------------------------------------------------------------------

def resolve_target(to: str, pane_opt: str | None, session_opt: str | None) -> tuple[str, Profile, str]:
    """Return (pane id, profile, label for the target). Never guesses between two candidates."""
    snapshot = agw.tree()
    mine = agw.my_pane()

    if pane_opt:
        found = agw.find_pane(pane_opt, snapshot)
        if not found:
            raise Refused(f"no pane {pane_opt} in this agwinterm window")
        tool = to if to in PROFILES else (hub.lookup(to) or {}).get("tool", "")
        if tool not in PROFILES:
            raise Refused(f"--pane needs --to claude|codex (or a registered box); got {to!r}")
        return pane_opt, PROFILES[tool], to

    entry = hub.lookup(to)
    if entry:
        pane = entry.get("pane")
        tool = entry.get("tool", "")
        if tool not in PROFILES:
            raise Refused(f"box {to!r} is registered as tool {tool!r}, which cannot be typed into")
        if not pane or not agw.find_pane(pane, snapshot):
            raise Refused(
                f"box {to!r} is registered on pane {pane} which is not open any more; "
                f"ask that agent to re-run `agmsg register`"
            )
        return pane, PROFILES[tool], to

    if to not in PROFILES:
        raise Refused(f"unknown target {to!r}: not a registered box and not claude|codex")

    # No registry entry: fall back to the split this process sits in, one peer per pane.
    if not mine:
        raise Refused("no AGWINTERM_PANE_ID: run this from inside an agwinterm pane, or pass --pane")
    located = agw.find_pane(mine, snapshot)
    if not located:
        raise Refused(f"own pane {mine} is not in the tree")
    _, session, _ = located
    if session_opt and session["id"] != session_opt:
        found = agw.find_session(session_opt, snapshot)
        if not found:
            raise Refused(f"no session {session_opt}")
        session = found[1]
    panes = agw.panes_of(session)
    others = [pane for pane in panes if pane != mine]
    if len(panes) < 2:
        raise Refused("this session has no split: two-agent chat needs one agent per pane")
    if len(others) != 1:
        raise Refused(f"{len(others)} candidate panes in this session; pass --pane <id>")
    return others[0], PROFILES[to], to


# --- sending --------------------------------------------------------------------------------

def precheck(pane: str, profile: Profile) -> None:
    text = agw.pane_text(pane)
    if dialog_visible(text):
        raise Refused("a chooser or approval dialog is on screen in the target pane")
    content = composer(profile, text)
    if content is None:
        raise Refused(f"no {profile.display} composer visible in the target pane")
    if not looks_empty(profile, content):
        raise Refused(f"the target composer is not empty: {content[:60]!r}")


def verify_typed(pane: str, profile: Profile, typed: str) -> None:
    """Prove the typed text reached the composer, or raise `Failed` and let the caller stop.

    What is compared is the FIRST `FRAGMENT` characters of the typed text - which, since every
    send is prefixed with "Chat from X: ", is the label plus about six characters of the message -
    against the whole joined composer content, wrapped rows included. It is evidence that the
    message started to land, not that all of it did.

    Widening it to the whole message would be wrong: Codex clips a long composer to a fixed number
    of visible rows, so a 1200-character send would fail verification with the text already in a
    live composer. This never raises `Refused` for the same reason - by the time it runs the text
    is in someone else's input box, and a retry would duplicate it.
    """
    fragment = normalize(typed)[:FRAGMENT]
    deadline = time.time() + VERIFY_TIMEOUT
    seen = ""
    while True:
        text = agw.pane_text(pane)
        # Re-checked here, not only in precheck: a dialog can appear between the two, and this is
        # the last look before the submit key goes in. Failed, never Refused - text is already in
        # the composer, so this must not be retried.
        if dialog_visible(text):
            raise Failed(
                "a chooser or approval dialog appeared in the target pane after the text was typed; "
                "submit withheld. Read the pane before doing anything else."
            )
        content = composer(profile, text)
        if content:
            seen = normalize(content)
            if fragment in seen:
                return
        if time.time() >= deadline:
            break
        time.sleep(0.25)
    raise Failed(
        "typed text was not confirmed in the target composer; submit withheld. "
        f"Looked for {fragment!r}; the pane now shows {seen[:120]!r} - "
        "read it before doing anything else."
    )


def send_once(pane: str, profile: Profile, text: str, *, dry_run: bool) -> int:
    reject_control_bytes(text)   # defence in depth: every path into a pane passes through here
    precheck(pane, profile)
    if dry_run:
        print(f"[dry-run] would type {len(text)} chars into pane {pane} "
              f"and submit with {'Tab' if profile.submit == chr(9) else 'Return'}")
        print(f"[dry-run] {text}")
        return 0
    agw.type_into(pane, text)
    time.sleep(SETTLE)
    verify_typed(pane, profile, text)
    agw.type_into(pane, profile.submit)
    return len(text)


def send(pane: str, profile: Profile, text: str, *, dry_run: bool, retry: bool) -> int:
    attempts = RETRY_ATTEMPTS if retry else 1
    for attempt in range(1, attempts + 1):
        try:
            return send_once(pane, profile, text, dry_run=dry_run)
        except Refused as refusal:
            if attempt == attempts:
                raise
            print(f"peer-chat: {refusal} (attempt {attempt}/{attempts}, retrying in "
                  f"{int(RETRY_DELAY)}s)", file=sys.stderr)
            time.sleep(RETRY_DELAY)
    raise Refused("unreachable")


def compose_text(label: str, message: str) -> str:
    """The exact text that goes into the other agent's composer.

    A seam, so the composition can be tested without a terminal. The label gets the same treatment
    as the message because it is typed the same way: whitespace in it - a newline above all - would
    press the peer's submit key before the message was written.
    """
    label = normalize(label)
    if label:
        label += " "
    text = label + normalize(message)
    reject_control_bytes(text)
    return text


def sender_display() -> str:
    me = hub.whoami()
    tool = (me or {}).get("tool") or hub.detect_tool()
    if tool in PROFILES:
        return PROFILES[tool].display
    return (me or {}).get("box") or "an agent"


def read_message(args: argparse.Namespace) -> str:
    if args.stdin:
        return sys.stdin.read()
    if args.message_file:
        return Path(args.message_file).read_text(encoding="utf-8")
    return args.text or ""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="type one line into the peer agent's composer")
    parser.add_argument("--to", required=True, help="a registered box name, or claude|codex")
    parser.add_argument("--pane", help="explicit target pane id (from `agwintermctl tree --json`)")
    parser.add_argument("--session", help="session whose split to use when --to is a bare tool name")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--stdin", action="store_true", help="read the message from stdin")
    source.add_argument("--text", help="the message as one paragraph")
    source.add_argument("--message-file", help="read the message from this file")
    parser.add_argument("--label", default=None,
                        help='override the "Chat from X: " prefix (empty string to send unlabelled)')
    parser.add_argument("--dry-run", action="store_true", help="run every check, type nothing")
    parser.add_argument("--no-retry", action="store_true", help="do not retry a busy composer")
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def main() -> int:
    args = parse_args()
    try:
        message = normalize(read_message(args))
        if not message:
            print("peer-chat: empty message", file=sys.stderr)
            return 1
        # The label is typed into the composer exactly like the message, so it gets the same
        # treatment: a newline in it presses the peer's submit key before the message is written.
        label = args.label if args.label is not None else f"Chat from {sender_display()}: "
        text = compose_text(label, message)
        if len(text) > MAX_TYPED:
            print(f"peer-chat: message is {len(text)} chars, over the {MAX_TYPED} typing limit. "
                  f"Put it in the inbox (`agmsg send ... --nudge`) and send a pointer instead.",
                  file=sys.stderr)
            return 1
        pane, profile, name = resolve_target(args.to, args.pane, args.session)
        if pane == agw.my_pane():
            print("peer-chat: refusing to type into my own pane", file=sys.stderr)
            return 1
        sent = send(pane, profile, text, dry_run=args.dry_run, retry=not args.no_retry)
        print(f'{{"sent": {sent}, "to": "{name}", "pane": "{pane}"}}')
        return 0
    except KeyboardInterrupt:
        return 130
    except (Refused, Failed, agw.CtlError, OSError) as err:
        print(f"peer-chat: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
