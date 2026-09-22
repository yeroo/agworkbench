#!/usr/bin/env python3
"""trust - record Claude Code's folder trust for one workbench clone.

Claude Code stops on "Do you trust the files in this folder?" the first time it starts in a new
directory, and `--dangerously-skip-permissions` does not skip it. Its highlighted default is
"No, exit". The relay will not answer a dialog, so every new issue would stop there - or end, if the
human hit Enter out of habit. github-workbench records the trust the way Claude does when you choose
"Yes, I trust this folder": `projects["<path>"].hasTrustDialogAccepted = true` in ~/.claude.json.

Only for a clone this tool created; the caller passes that path and nothing else.

~/.claude.json is written by every running Claude process, with no lock file. So: read, change one
key, write to a temp file in the same directory, and replace only if the file has not been modified
since it was read - otherwise start again. Python, not PowerShell: ConvertTo-Json in Windows
PowerShell 5.1 defaults to depth 2 and would silently flatten the nested objects in that file.

  trust.py --claude <clone-dir>
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


def claude_key(directory: str) -> str:
    """Claude Code keys projects by the path with forward slashes, drive letter as given."""
    return str(Path(directory).resolve()).replace("\\", "/")


def grant_claude(directory: str, config: Path | None = None, attempts: int = 8) -> str:
    config = config or (Path.home() / ".claude.json")
    key = claude_key(directory)
    for _ in range(attempts):
        if config.exists():
            before = config.stat().st_mtime_ns
            data = json.loads(config.read_text(encoding="utf-8"))
        else:
            before, data = None, {}
        projects = data.setdefault("projects", {})
        entry = projects.get(key) or {}
        if entry.get("hasTrustDialogAccepted"):
            return f"already trusted: {key}"
        entry["hasTrustDialogAccepted"] = True
        projects[key] = entry
        tmp = config.with_name(config.name + f".agworkbench-{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        now = config.stat().st_mtime_ns if config.exists() else None
        if now != before:
            tmp.unlink(missing_ok=True)       # a Claude process wrote in between: re-read and retry
            time.sleep(0.2)
            continue
        os.replace(tmp, config)
        return f"trusted: {key}"
    raise RuntimeError(f"~/.claude.json kept changing under us; trust not recorded for {key}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--claude", required=True, help="the clone directory to trust")
    parser.add_argument("--config", help="path to .claude.json (tests)")
    args = parser.parse_args()
    try:
        print(grant_claude(args.claude, Path(args.config) if args.config else None))
        return 0
    except (OSError, ValueError, RuntimeError) as err:
        print(f"trust: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
