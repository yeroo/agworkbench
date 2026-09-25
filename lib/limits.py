#!/usr/bin/env python3
"""limits - recognise an agent pane that has hit its usage limit (#24).

Pure: `classify(text, tool)` looks at one pane frame and returns None, a `warning` (Codex's
"Approaching rate limits" chooser) or `limited` (the agent's own limit message, or the agent has
exited to a shell right after one). The relay calls it on every limit tick; the launcher's
`-Failover` calls the CLI below on a fresh read before it stops anything.

False positives cost more than misses - this very feature puts these phrases on screen in diffs,
greps, test output and fixture dumps - so a phrase counts only by POSITION, never by presence:

- Claude alive: the phrase starts the last item above an idle composer (only blank rows below
  it), and that item is not a tool result (a `⎿` row whose parent is a `● Tool(...)` call).
- Codex alive: the phrase starts a row of the last block above the composer, and that row is not
  tool output (`└`, `│`, `├`).
- Either tool exited: the last row is a shell prompt and the phrase starts one of the rows just
  above it, in the output since the previous prompt.

A row starts with the phrase after optional indentation and at most one status glyph. A quote,
backtick, `+`, `-`, `>`, `#`, `|`, or a `path:` prefix in front of it (diffs, code, greps) never
counts.

  python lib/limits.py classify --tool codex < frame.txt     # prints JSON
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass

WINDOW = 20      # rows considered: the last 20 non-empty rows of the frame

APOSTROPHES = re.compile("[\u2018\u2019\u02bc]")
# One leading status glyph: any single non-alphanumeric, non-space character except the ones that
# mark quoted or diffed text, followed by whitespace.
GLYPH = r"(?:[^\w\s\"'`+\->#|:]\s+)?"
FORBIDDEN_TOOL_OUTPUT = ("└", "│", "├")
# Claude Code starts an item (a message, a tool call, a tool result, a notice) with one of these;
# any other row is a continuation of the item above it.
CLAUDE_ITEM_RE = re.compile(r"^\s*[●⎿>❯✻✳✶✢✽⚠·]\s")
# Codex draws each history cell from column 0 behind its glyph; indented rows belong to a cell.
CODEX_CELL_RE = re.compile(r"^[^\s\w]\s")

LIMITED = {
    "codex": [
        r"You've hit your usage limit\b",
        r"Usage limit reached\b",
        r"You've reached your (?:usage|workspace credit) limit\b",
        r"(?:You're|Your workspace is) out of credits\b",
    ],
    "claude": [
        r"You've hit your (?:[\w'-]+ ){0,3}(?:limit|budget)\b",
        r"Usage limit reached\b",
        r"usage credit limit reached\b",
        r"Out of usage credits\b",
    ],
}
WARNING_CODEX = [r"Approaching rate limits\b", r"Heads up, you have less than \d+% of your \w+ limit left\b",
                 r"Switch to \S+ for lower credit usage\?"]
CHOOSER_ROW = re.compile(r"^\s*[›>❯]?\s*\d\.\s+(?:Switch to|Keep current model)")

RULE_RE = re.compile(r"^\s*[─━—-]{10,}\s*$")
CLAUDE_PROMPT_RE = re.compile(r"^\s*[>❯]\s?")
CODEX_PROMPT_RE = re.compile(r"^[›»]")
TOOL_CALL_RE = re.compile(r"^\s*●\s+[A-Za-z_][\w.-]*\(")
SHELL_PS_RE = re.compile(r"^PS [A-Za-z]:\\[^>]*> ?$")
SHELL_PS_COMMAND_RE = re.compile(r"^PS [A-Za-z]:\\[^>]*>")   # a prompt, with or without a command after it
SHELL_GLYPH_RE = re.compile(r"^\s*❯\s*$")
SHELL_TIMING_RE = re.compile(r"(\d+(\.\d+)?(ms|s)|\d\d:\d\d(:\d\d)?)\s*$")
BUSY_RE = re.compile(r"esc to interrupt|…\s*\((?:\d+h )?(?:\d+m )?\d+s\s*·", re.IGNORECASE)


@dataclass(frozen=True)
class Limit:
    kind: str            # "warning" | "limited"
    line: str            # the matched row, stripped
    exited: bool = False


def _normal(row: str) -> str:
    return APOSTROPHES.sub("'", row)


def _starts_with(row: str, patterns: list[str]) -> bool:
    text = _normal(row).strip()
    return any(re.match(GLYPH + pattern, text, re.IGNORECASE) for pattern in patterns)


def _rows(text: str) -> list[str]:
    """The frame's rows from the WINDOW-th last non-empty one, blank rows kept."""
    rows = (text or "").splitlines()
    while rows and not rows[-1].strip():
        rows.pop()
    count = 0
    for start in range(len(rows) - 1, -1, -1):
        if rows[start].strip():
            count += 1
            if count == WINDOW:
                return rows[start:]
    return rows


def tail_hash(text: str) -> str:
    """A fingerprint of the last WINDOW non-empty rows, for "has the pane changed" checks."""
    rows = [row.rstrip() for row in _rows(text) if row.strip()]
    return hashlib.sha1("\n".join(rows).encode("utf-8")).hexdigest()


def shell_prompt(rows: list[str]) -> bool:
    """The launcher's Test-ShellReady rule for the last row."""
    filled = [row for row in rows if row.strip()]
    if not filled:
        return False
    if SHELL_PS_RE.match(filled[-1]):
        return True
    return bool(SHELL_GLYPH_RE.match(filled[-1]) and len(filled) >= 2 and SHELL_TIMING_RE.search(filled[-2]))


def _last_block(rows: list[str]) -> list[str]:
    while rows and not rows[-1].strip():
        rows = rows[:-1]
    start = len(rows)
    while start and rows[start - 1].strip():
        start -= 1
    return rows[start:]


def _exited(rows: list[str], tool: str) -> Limit | None:
    filled = [row for row in rows if row.strip()]
    # The output since the previous prompt, i.e. of the command that ran the agent.
    output: list[str] = []
    for row in reversed(filled[:-1]):
        if SHELL_PS_COMMAND_RE.match(row) or SHELL_GLYPH_RE.match(row):
            break
        output.insert(0, row)
    for row in reversed(output[-8:]):
        if _starts_with(row, LIMITED[tool]) and not row.strip().startswith(FORBIDDEN_TOOL_OUTPUT):
            return Limit("limited", row.strip(), exited=True)
    return None


def _claude(rows: list[str]) -> Limit | None:
    rules = [i for i, row in enumerate(rows) if RULE_RE.match(row)]
    if len(rules) < 2 or not CLAUDE_PROMPT_RE.match(rows[rules[-2] + 1] if rules[-2] + 1 < len(rows) else ""):
        return None
    if BUSY_RE.search("\n".join(rows)):
        return None                        # a running turn: any limit phrase on screen is old output
    above = rows[:rules[-2]]
    while above and not above[-1].strip():
        above = above[:-1]
    block = _last_block(above)
    if not block:
        return None
    # The last item: its first row carries a glyph; continuation rows are indented text.
    starts = [i for i, row in enumerate(block) if CLAUDE_ITEM_RE.match(row)]
    if not starts:
        return None
    item = starts[-1]
    row = block[item]
    if not _starts_with(row, LIMITED["claude"]):
        return None
    if row.strip().startswith("⎿"):
        # Its parent is the nearest item above that is not itself a result: a user prompt or a
        # notice for a real limit, a `● Tool(...)` call for output that merely contains the phrase.
        before = above[:len(above) - len(block) + item]
        parent = next((r for r in reversed(before)
                       if CLAUDE_ITEM_RE.match(r) and not r.strip().startswith("⎿")), "")
        if TOOL_CALL_RE.match(parent):
            return None
    return Limit("limited", row.strip())


def _codex(rows: list[str]) -> Limit | None:
    prompts = [i for i, row in enumerate(rows) if CODEX_PROMPT_RE.match(row)]
    if not prompts:
        return None
    above = rows[:prompts[-1]]
    if BUSY_RE.search("\n".join(rows[prompts[-1]:])) or any("Working" in row for row in rows[-4:]):
        return None
    for row in reversed(_last_block(above)):
        if not CODEX_CELL_RE.match(row) or row.strip().startswith(FORBIDDEN_TOOL_OUTPUT):
            continue               # tool output and other rows inside a cell
        if _starts_with(row, LIMITED["codex"]):
            return Limit("limited", row.strip())
    return None


def _codex_warning(rows: list[str]) -> Limit | None:
    if not any(CHOOSER_ROW.match(row) for row in rows):
        return None
    for row in rows:
        if _starts_with(row, WARNING_CODEX):
            return Limit("warning", row.strip())
    return None


def classify(text: str, tool: str) -> Limit | None:
    if tool not in LIMITED:
        raise ValueError(f"unknown tool {tool!r}")
    rows = _rows(text)
    if not rows:
        return None
    if shell_prompt(rows):
        return _exited(rows, tool)
    if tool == "codex":
        return _codex_warning(rows) or _codex(rows)
    return _claude(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="limits")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("classify", help="classify a pane frame read from stdin; prints JSON")
    p.add_argument("--tool", required=True, choices=sorted(LIMITED))
    args = parser.parse_args(argv)
    text = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    found = classify(text, args.tool)
    result = {"kind": found.kind if found else None, "line": found.line if found else None,
              "exited": bool(found and found.exited), "tail": tail_hash(text)}
    sys.stdout.write(json.dumps(result) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
