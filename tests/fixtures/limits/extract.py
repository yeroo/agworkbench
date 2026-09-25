#!/usr/bin/env python3
"""Extract the usage-limit strings from the installed agent binaries into strings-<tool>.txt.

Run once when a new Codex or Claude Code release changes its wording, then review the diff:

  python tests/fixtures/limits/extract.py --codex <codex.exe> --claude <claude.exe>

Each output line is one printable run from the binary that contains a limit phrase, exactly as
stored (UTF-8 text in both binaries). The frame fixtures next to this file quote their limit rows
from these files, and tests/test_limits.py checks that they do - so no limit string in the tests
was retyped by hand.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
PHRASES = {
    "codex": [b"ve hit your usage limit", b"Usage limit reached", b"out of credits", b"reached your usage limit",
              b"Approaching rate limits", b"Heads up, you have less than", b"for lower credit usage?",
              b"Keep current model", b"due to usage limits"],
    "claude": [b"You've hit your", b"Usage limit reached", b"usage credit limit reached",
               b"Out of usage credits", b'five_hour:"session limit"'],
}
# Printable ASCII plus the UTF-8 lead/continuation bytes, so "·" and "—" survive.
RUN = re.compile(rb"[\x20-\x7e\x80-\xf4]{4,400}")


def extract(binary: Path, phrases: list[bytes]) -> list[str]:
    data = binary.read_bytes()
    found: set[str] = set()
    for run in RUN.finditer(data):
        chunk = run.group(0)
        if any(phrase in chunk for phrase in phrases):
            found.add(chunk.decode("utf-8", errors="replace"))
    return sorted(found)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex", type=Path, required=True)
    parser.add_argument("--claude", type=Path, required=True)
    args = parser.parse_args()
    for tool, binary in (("codex", args.codex), ("claude", args.claude)):
        lines = extract(binary, PHRASES[tool])
        (HERE / f"strings-{tool}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        print(f"{tool}: {len(lines)} runs from {binary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
