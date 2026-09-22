#!/usr/bin/env python3
"""post - file one message into a workbench mailbox as a named, non-agent sender.

The review helpers (revmux, revdiff) are not agents and have no box of their own; registering one
for them would put a fake agent in the registry. They post under a sender name instead, and the
relay rings the recipient.

  post.py --hub <dir> --to claude --sender revmux --kind review --subject "..." --body-file report.md
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hub", required=True)
    parser.add_argument("--to", required=True)
    parser.add_argument("--sender", required=True)
    parser.add_argument("--subject", required=True)
    parser.add_argument("--kind", default="message")
    parser.add_argument("--body-file")
    parser.add_argument("--text")
    args = parser.parse_args()

    os.environ["AI_HUB"] = args.hub
    import hub
    hub.reload_paths()
    body = args.text or ""
    if args.body_file:
        path = Path(args.body_file)
        body = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    if not body.strip():
        body = "(empty)"
    path = hub.write_message(to=args.to, sender=args.sender, subject=args.subject, body=body,
                             kind=args.kind)
    print(f"posted {path.name} -> {args.to}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
