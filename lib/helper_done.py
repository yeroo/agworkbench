#!/usr/bin/env python3
"""helper_done - a helper session's completion marker (#33).

run-revmux.ps1 and human-review.ps1 call this as their very last act (in `finally`). It records
that the helper finished and what its pane showed at that moment, so the autonomous close can prove
the pane has not been touched since (see closer.helper_untouched):

  python lib/helper_done.py --hub <checkout>\\.workbench --kind revmux --round 2 --exit 1

The marker is keyed by the helper's own pane id, which is also the session id of a one-pane helper.
A helper that is killed writes none, and stays open.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import agw  # noqa: E402
import closer  # noqa: E402


def write_marker(hub: str | Path, kind: str, *, round: int | None = None, exit: int | None = None,
                 failures: int | None = None) -> Path | None:
    """Write this pane's completion marker; the caller's very last act. None outside agwinterm.
    run_helper.py (#45) calls it in-process, after its last line is printed and flushed."""
    pane = os.environ.get("AGWINTERM_PANE_ID") or os.environ.get("AGWINTERM_SESSION_ID")
    if not pane:
        print("helper_done: not inside an agwinterm pane; no marker written", file=sys.stderr)
        return None
    try:
        rows = closer.filled_rows(agw.pane_text(pane))
    except (agw.CtlError, OSError) as err:
        # Without the rows the pane cannot be proven untouched: the close will leave it open.
        print(f"helper_done: cannot read this pane ({err}); the marker cannot prove it untouched", file=sys.stderr)
        rows = []
    marker = {"kind": kind, "round": round, "exit": exit, "pane": pane, "at": time.time(), "rows": rows}
    if failures is not None:
        marker["failures"] = failures
    directory = Path(hub) / "state" / "helpers"
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / f".{pane}.tmp"
    temporary.write_text(json.dumps(marker, indent=2), encoding="utf-8")
    target = directory / f"{pane}.done"
    os.replace(temporary, target)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="helper_done")
    parser.add_argument("--hub", required=True)
    parser.add_argument("--kind", required=True, choices=("revmux", "review", "suite"))
    parser.add_argument("--round", type=int)
    parser.add_argument("--exit", type=int)
    parser.add_argument("--failures", type=int)
    args = parser.parse_args(argv)
    write_marker(args.hub, args.kind, round=args.round, exit=args.exit, failures=args.failures)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
