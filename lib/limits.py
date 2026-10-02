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
- Kimi Code alive (#65): its idle composer box is at the bottom (no spinner row above it), and the
  last rows above the box are a session error - `Error: [<code>] <message>`, possibly wrapped -
  followed by the report hint Kimi always draws after one ("If this persists, run
  `/export-debug-zip`..."). The message must name a quota or usage limit: a bare
  `[provider.rate_limit]` is a transient 429 that Kimi already retried, not a reason to fail over.
  Kimi draws that status row with no glyph and no blank row above it, at the same indent as a
  message continuation, so the hint row below it is what places it. Under a tool call the rows count
  only when the call's output provably ended above them (#88): a successful call collapsed to ONE row
  marked `…` right above the error, or a status row wrapped to column 1 (output rows never wrap). The
  error may wrap over up to 8 rows and the hint over 3. `kimi_turn_limit` drops the owner rule for the
  relay's safety nets (the stall watch, a forced wait episode). Kimi docks its todo panel between the
  transcript (and the spinner) and the box (#88, docxy #820): `kimi_without_todo` removes exactly that
  panel first, and the window counts only the rows above it. A panel clipped in a short pane is not
  that exact shape, so it stays and hides the error: a miss, never a false limit.
- Any tool exited: the last row is a shell prompt and the phrase starts one of the rows just
  above it, in the output since the previous prompt.

A limit row starts with the phrase right after the ONE glyph that tool draws its own notices
with: `⎿` for Claude (a result row, whose parent is checked), `■` for a Codex error cell. An
agent's own reply (`●` for Claude, `•` for Codex) that happens to begin with the phrase never
counts, nor does any other prefix (quotes, diffs, code, greps). Codex's warning rows carry `⚠`
or no glyph, and count only above the chooser at the bottom of the pane (#61).

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
# The glyph each tool draws its own limit notice with (r17: an agent's reply glyph never counts).
LIMIT_GLYPH = {"claude": r"⎿\s+", "codex": r"■\s+", "kimi": r""}
WARNING_GLYPH = r"(?:⚠\s+)?"
FORBIDDEN_TOOL_OUTPUT = ("└", "│", "├")
# Claude Code starts an item (a message, a tool call, a tool result, a notice) with one of these;
# any other row is a continuation of the item above it.
CLAUDE_ITEM_RE = re.compile(r"^\s*[●⎿>❯✻✳✶✢✽⚠·]\s")
# Codex draws each history cell from column 0 behind its glyph; indented rows belong to a cell.
CODEX_CELL_RE = re.compile(r"^[^\s\w]\s")

# Kimi's quota wording, without the session error's `Error: [provider.<x>]` prefix: wb.py's blocked-reason
# check (#88) matches it in free text too.
KIMI_QUOTA = (r"exceeded_current_quota_error|exceeded your current (?:token )?quota"
              r"|insufficient balance|check your account balance|recharge your account|please recharge"
              r"|account (?:is )?in arrears|usage limit|(?:weekly|daily|monthly|hourly|5-hour|plan) limit")

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
    # Kimi's quota code and message patterns (KIMI_QUOTA_EXHAUSTED_*, see strings-kimi.txt), plus the
    # words a plan's usage limit would use, anywhere in the session error's message.
    "kimi": [r"Error: \[provider\.[a-z_]+\] .*?(?:" + KIMI_QUOTA + ")"],
}
WARNING_CODEX = [r"Approaching rate limits\b", r"Heads up, you have less than \d+% of your \w+ limit left\b",
                 r"Switch to \S+ for lower credit usage\?"]
CHOOSER_ROW = re.compile(r"^\s*[›>❯]?\s*\d\.\s+(?:Switch to|Keep current model)")
CHOOSER_HINT = re.compile(r"^\s*Press enter to confirm\b", re.IGNORECASE)

RULE_RE = re.compile(r"^\s*[─━—-]{10,}\s*$")
CLAUDE_PROMPT_RE = re.compile(r"^\s*[>❯]\s?")
CODEX_PROMPT_RE = re.compile(r"^[›»]")
TOOL_CALL_RE = re.compile(r"^\s*●\s+[A-Za-z_][\w.-]*\(")
SHELL_PS_RE = re.compile(r"^PS [A-Za-z]:\\[^>]*> ?$")
SHELL_PS_COMMAND_RE = re.compile(r"^PS [A-Za-z]:\\[^>]*>")   # a prompt, with or without a command after it
SHELL_GLYPH_RE = re.compile(r"^\s*❯\s*$")
SHELL_TIMING_RE = re.compile(r"(\d+(\.\d+)?(ms|s)|\d\d:\d\d(:\d\d)?)\s*$")
BUSY_RE = re.compile(r"esc to interrupt|…\s*\((?:\d+h )?(?:\d+m )?\d+s\s*·", re.IGNORECASE)
# Kimi Code: its composer box, the spinner row a running turn draws right above it (its
# "Retrying (n/10)" label too), the session error row and the hint row that always follows it.
KIMI_BOTTOM_RE = re.compile(r"^\s*╰─+╯\s*$")
KIMI_TOP_RE = re.compile(r"^\s*╭")
KIMI_ROW_RE = re.compile(r"^\s*│ ")
KIMI_SPINNER_RE = re.compile(r"^\s*[⠀-⣿\U0001F311-\U0001F318]\s")
KIMI_ERROR_RE = re.compile(r"^\s{1,4}Error: \[")
KIMI_HINT_RE = re.compile(r"^\s*If this persists, run `/export-debug-zip`")
KIMI_ITEM_RE = re.compile(r"^\s*[●✗✨$]\s")
# A tool call's header row: its output rows follow it at the same indent as a status row would.
KIMI_TOOL_RE = re.compile(r"^\s*(?:✗\s|●\s+(?:Ran|Running|Used|Using|Read|Reading|Wrote|Writing|Edited|Editing"
                          r"|Searched|Searching|Fetched|Fetching)\b)")
# Kimi's todo panel (TodoPanelComponent.render, #88): a rule, `Todo`, one row per todo marked `●` in
# progress, `✓` done or `○` pending, then the collapsed panel's overflow row or the expanded one's footer.
KIMI_TODO_RULE_RE = re.compile(r"^\s*─{10,}\s*$")
KIMI_TODO_HEAD_RE = re.compile(r"^\s*Todo\s*$")
KIMI_TODO_ROW_RE = re.compile(r"^\s*[●✓○] ")
KIMI_TODO_MORE_RE = re.compile(r"^\s*(?:… \+\d+ more\b.* · ctrl\+t to expand|all \d+ items · ctrl\+t to collapse)\s*$")


@dataclass(frozen=True)
class Limit:
    kind: str            # "warning" | "limited"
    line: str            # the matched row, stripped
    exited: bool = False


def _normal(row: str) -> str:
    return APOSTROPHES.sub("'", row)


def _starts_with(row: str, patterns: list[str], glyph: str) -> bool:
    text = _normal(row).strip()
    return any(re.match(glyph + pattern, text, re.IGNORECASE) for pattern in patterns)


def _rows(text: str) -> list[str]:
    """The frame's rows from the WINDOW-th last non-empty one, blank rows kept."""
    return _window((text or "").splitlines(), WINDOW)


def _window(rows: list[str], size: int) -> list[str]:
    """`rows` from the `size`-th last non-empty one, trailing blank rows dropped."""
    if size <= 0:
        return []
    rows = list(rows)
    while rows and not rows[-1].strip():
        rows.pop()
    count = 0
    for start in range(len(rows) - 1, -1, -1):
        if rows[start].strip():
            count += 1
            if count == size:
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


def ps_prompt_last(text: str | None) -> bool:
    """#98: the frame's last non-empty row is a bare pwsh prompt (`PS X:\\...> `, nothing typed after it).
    Rows above it are not looked at: an agent that crashed leaves its whole frame there."""
    filled = [row.rstrip() for row in (text or "").splitlines() if row.strip()]
    return bool(filled) and bool(SHELL_PS_RE.match(filled[-1]))


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
        if _starts_with(row, LIMITED[tool], LIMIT_GLYPH[tool]):
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
    if not _starts_with(row, LIMITED["claude"], LIMIT_GLYPH["claude"]):
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
        if _starts_with(row, LIMITED["codex"], LIMIT_GLYPH["codex"]):
            return Limit("limited", row.strip())
    return None


def _codex_warning(rows: list[str]) -> Limit | None:
    """The chooser replaces Codex's composer, so it counts only at the bottom of the pane (#61): the
    last row is an option or the confirm hint, the options run up from there, and a warning row sits
    above them before any other cell or tool output. A dump of the chooser in history has the
    composer and its footer below it."""
    filled = [row for row in rows if row.strip()]
    end = len(filled)
    if end and CHOOSER_HINT.match(filled[-1]):
        end -= 1
    start = end
    while start and CHOOSER_ROW.match(filled[start - 1]):
        start -= 1
    if start == end:
        return None
    found = None
    for row in reversed(filled[:start]):
        if _starts_with(row, WARNING_CODEX, WARNING_GLYPH):
            found = row                # keep climbing: the topmost warning row names the episode
        elif CODEX_CELL_RE.match(row) or row.strip().startswith(FORBIDDEN_TOOL_OUTPUT):
            break                      # another history cell or tool output: the chooser ends here
    return Limit("warning", found.strip()) if found else None


def _kimi_collapsed(row: str) -> bool:
    """One outcome row standing for a longer output (Kimi 2.1.1 outcomeLine): `  … last line` or `  first
    line …`. The marker sits in a fixed head or tail, a space away from the text, so a width cut
    (`by na…`) is not one."""
    text = row.strip()
    return text.startswith("… ") or text.endswith(" …")


def kimi_without_todo(above: list[str]) -> list[str]:
    """`above` (the rows above Kimi's composer box) without the todo panel docked at its end (#88), and
    the blank rows before it. Unchanged when the end is not exactly a panel: a rule row, `Todo`, at
    least one todo row, at most one overflow or footer row, and nothing else."""
    end = len(above)
    while end and not above[end - 1].strip():
        end -= 1
    if end and KIMI_TODO_MORE_RE.match(above[end - 1]):
        end -= 1
    start = end
    while start and KIMI_TODO_ROW_RE.match(above[start - 1]):
        start -= 1
    if (start == end or start < 2 or not KIMI_TODO_HEAD_RE.match(above[start - 1])
            or not KIMI_TODO_RULE_RE.match(above[start - 2])):
        return list(above)
    rest = list(above[:start - 2])
    while rest and not rest[-1].strip():
        rest.pop()
    return rest


def _kimi(frame: list[str], owner_check: bool = True) -> Limit | None:
    """`frame` is the whole pane. The window is taken above the box and the todo panel (#88), so a panel
    never crowds the error out of it; without a panel these are the rows of the frame's last WINDOW."""
    rows = list(frame)
    while rows and not rows[-1].strip():
        rows.pop()
    filled = [i for i, row in enumerate(rows) if row.strip()]
    bottom = next((i for i in reversed(filled) if KIMI_BOTTOM_RE.match(rows[i])), None)
    if bottom is None or sum(1 for i in filled if i > bottom) > 3:
        return None                        # no composer at the bottom: a dialog, or not Kimi at all
    top = bottom - 1
    while top >= 0 and KIMI_ROW_RE.match(rows[top]):
        top -= 1
    if top < 0 or top == bottom - 1 or not KIMI_TOP_RE.match(rows[top]):
        return None
    # The box and its footer take their rows of the window; the todo panel takes none.
    above = _window(kimi_without_todo(rows[:top]), WINDOW - sum(1 for i in filled if i >= top))
    if not above or KIMI_SPINNER_RE.match(above[-1]):
        return None                        # a running turn, or its retries: any error on screen is old
    # The hint is the last row, or starts up to 2 rows above it when it wraps (#88: 3 rows at 60 columns).
    hint = next((i for i in range(len(above) - 1, max(len(above) - 4, -1), -1) if KIMI_HINT_RE.match(above[i])), None)
    if hint is None or any(not row.strip() or KIMI_ITEM_RE.match(row) for row in above[hint + 1:]):
        return None
    # The error wraps over up to 7 rows below it in a narrow pane (#88).
    for start in range(hint - 1, max(hint - 9, -1), -1):
        row = above[start]
        if KIMI_ERROR_RE.match(row):
            if owner_check and not _kimi_owner_allows(above, start):
                return None
            message = " ".join(part.strip() for part in above[start:hint])
            if _starts_with(message, LIMITED["kimi"], LIMIT_GLYPH["kimi"]):
                return Limit("limited", row.strip())
            return None
        if not row.strip() or KIMI_ITEM_RE.match(row):
            return None                    # the hint follows something else: not a session error
    return None


def _kimi_owner_allows(above: list[str], start: int) -> bool:
    """The status row is glued to the item above it, so that item decides: a message or a prompt is
    where a session error lands. Under a tool call the rows may be its output (#65), unless that output
    provably ended above them (#88): a successful call (`●`) draws at most 3 rows, each cut to one row,
    or ONE row marked with `…` - so the error right after that one marked row is not output - and a
    status row wraps to column 1, where no output row ever starts. A failed call (`✗`) draws its whole
    output, as does one expanded with ctrl+o, so its rows are never placed."""
    owner = next((i for i in range(start - 1, -1, -1) if KIMI_ITEM_RE.match(above[i])), None)
    if owner is None or not KIMI_TOOL_RE.match(above[owner]):
        return True
    if above[owner].lstrip().startswith("✗"):
        return False
    if start == owner + 2 and _kimi_collapsed(above[owner + 1]):
        return True
    return any(len(row) - len(row.lstrip(" ")) == 1 for row in above[start:])


def kimi_turn_limit(text: str) -> Limit | None:
    """#88: the frame ends in a Kimi limit session error, whatever item owns it - `_kimi`'s positional
    rules without the owner check. Only for the safety nets, where other evidence already stands: the
    stall watch (15 idle minutes) and an episode the relay already waits out."""
    rows = _rows(text)
    if not rows or shell_prompt(rows):
        return None
    return _kimi(text.splitlines(), owner_check=False)


def classify(text: str, tool: str) -> Limit | None:
    if tool not in LIMITED:
        raise ValueError(f"unknown tool {tool!r}")
    rows = _rows(text)
    if not rows:
        return None
    if shell_prompt(rows):
        # Kimi stays alive on a quota error (it draws it above its composer); an exited Kimi is a
        # stall for the planner, and a quota row left on screen proves nothing (FIX r2 m3).
        return None if tool == "kimi" else _exited(rows, tool)
    if tool == "codex":
        return _codex_warning(rows) or _codex(rows)
    if tool == "kimi":
        return _kimi(text.splitlines())
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
