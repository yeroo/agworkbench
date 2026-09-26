#!/usr/bin/env python3
"""run_helper - a long-running helper (the whole suite, a build) that finishes where agents can see it (#45).

wb.py suite opens this in its own visible session, in agwinterm's direct mode:

  python lib/run_helper.py --hub <checkout>\\.workbench --label 1f04542 --to claude -- python -m unittest discover -s tests

It runs the command with no shell around it (argv as given; a `.ps1` gets `pwsh -File`), echoes its
output to the pane, and writes it to `.workbench/review/suite-<label>.log` as UTF-8 without a BOM,
whatever the child wrote: Windows PowerShell 5.1's `>` redirection wrote UTF-16, which a text match
never sees. When the command ends it mails the result to `--to` (sender `helper`) - the relay rings
that pane, so nobody depends on a private background watcher that low memory can kill - and, as its
very last act, writes its completion marker (helper_done.write_marker) with the exit code and the
failure count, so the autonomous close can prove its pane untouched.
"""

from __future__ import annotations

import argparse
import codecs
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

LABEL_RE = re.compile(r"[A-Za-z0-9._-]+")
TAIL_LINES = 40           # log lines quoted in the result mail
CHUNK = 4096

SUITE_FAILURES_RE = re.compile(r"SUITE FAILURES:\s*(\d+)")
UNITTEST_FAILED_RE = re.compile(r"^FAILED \(([^)]*)\)\s*$")
UNITTEST_OK_RE = re.compile(r"^OK(?: \([^)]*\))?\s*$")
COUNT_FAILED_RE = re.compile(r"\b(\d+) failed\b")
COUNT_ERRORS_RE = re.compile(r"\b(\d+) errors?\b")


def log_path(hub: Path, label: str) -> Path:
    return Path(hub) / "review" / f"suite-{label}.log"


def command_for(argv: list[str]) -> list[str]:
    """argv as given, except that a PowerShell script runs under pwsh (Windows PowerShell as a fallback)."""
    if argv and argv[0].lower().endswith(".ps1"):
        shell = shutil.which("pwsh") or shutil.which("powershell.exe") or "powershell.exe"
        return [shell, "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", *argv]
    return list(argv)


def pick_decoder(first: bytes):
    """UTF-16 when the child writes it (a BOM, or NULs in alternate bytes), else UTF-8 that never raises."""
    if first.startswith(codecs.BOM_UTF16_LE) or first.startswith(codecs.BOM_UTF16_BE):
        return codecs.getincrementaldecoder("utf-16")(errors="replace")
    pairs = len(first) // 2
    if pairs >= 2:
        odd_nuls = sum(1 for i in range(1, pairs * 2, 2) if first[i] == 0)
        even_nuls = sum(1 for i in range(0, pairs * 2, 2) if first[i] == 0)
        if odd_nuls * 4 >= pairs * 3 and even_nuls * 4 < pairs:
            return codecs.getincrementaldecoder("utf-16-le")(errors="replace")
        if even_nuls * 4 >= pairs * 3 and odd_nuls * 4 < pairs:
            return codecs.getincrementaldecoder("utf-16-be")(errors="replace")
    return codecs.getincrementaldecoder("utf-8-sig")(errors="replace")


def count_failures(text: str) -> int | None:
    """The failure count a suite reported, from its last summary line; None when none is recognised.
    Recognised: `SUITE FAILURES: N`, unittest's `FAILED (failures=a, errors=b)` / `OK`, and a
    pytest or cargo summary with `N failed` (plus `M errors`)."""
    found = None
    for line in text.splitlines():
        line = line.strip()
        match = SUITE_FAILURES_RE.search(line)
        if match:
            found = int(match.group(1))
            continue
        match = UNITTEST_FAILED_RE.match(line)
        if match:
            # Only `failures` and `errors`: `expected failures=2` and `skipped=3` are not failures.
            parts = [part.strip().partition("=") for part in match.group(1).split(",")]
            found = sum(int(value) for key, _, value in parts
                        if key.strip() in ("failures", "errors") and value.strip().isdigit())
            continue
        if UNITTEST_OK_RE.match(line):
            found = 0
            continue
        failed = COUNT_FAILED_RE.search(line)
        if failed:
            errors = COUNT_ERRORS_RE.search(line)
            found = int(failed.group(1)) + (int(errors.group(1)) if errors else 0)
    return found


def run(argv: list[str], log: Path, echo) -> tuple[int | None, str]:
    """Run argv, echo and log its merged output as it arrives. Returns (exit code or None, the text)."""
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    log.parent.mkdir(parents=True, exist_ok=True)
    parts: list[str] = []
    with log.open("w", encoding="utf-8", newline="") as handle:
        def emit(text: str) -> None:
            if text:
                parts.append(text)
                handle.write(text)
                handle.flush()          # readable while it runs
                echo(text)
        try:
            child = subprocess.Popen(command_for(argv), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
        except OSError as err:
            emit(f"run_helper: could not start {argv[0]}: {err}\n")
            return None, "".join(parts)
        decoder = None
        try:
            while True:
                chunk = child.stdout.read1(CHUNK) if hasattr(child.stdout, "read1") else child.stdout.read(CHUNK)
                if not chunk:
                    break
                if decoder is None:
                    decoder = pick_decoder(chunk)
                emit(decoder.decode(chunk))
            if decoder is not None:
                emit(decoder.decode(b"", final=True))
            return child.wait(), "".join(parts)
        except KeyboardInterrupt:
            child.kill()
            child.wait()
            emit("\nrun_helper: interrupted; the command was stopped\n")
            return None, "".join(parts)
        finally:
            child.stdout.close()


def result_subject(label: str, code: int | None, failures: int | None) -> str:
    if code is None:
        return f"suite {label}: FAILED (did not finish)"
    detail = f"exit {code}" + (f", {failures} failure{'s' if failures != 1 else ''}" if failures is not None else "")
    passed = code == 0 and not failures
    return f"suite {label}: {'passed' if passed else 'FAILED'} ({detail})"


def post_result(hub: Path, to: str, subject: str, argv: list[str], code, failures, log: Path, text: str) -> str:
    os.environ["AI_HUB"] = str(hub)
    import hub as mailbox
    mailbox.reload_paths()
    tail = text.splitlines()[-TAIL_LINES:]
    body = "\n".join([f"Command: {subprocess.list2cmdline(argv)}",
                      f"Exit: {code if code is not None else 'none (it did not finish)'}",
                      f"Failures: {failures if failures is not None else 'not reported'}",
                      f"Log (UTF-8): {log}", "", f"Last {len(tail)} lines of the log:", "", "```", *tail, "```"])
    path = mailbox.write_message(to=to, sender="helper", kind="note", subject=subject, body=body)
    return path.stem


def echo_to_pane(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (ValueError, OSError):
                pass
    parser = argparse.ArgumentParser(prog="run_helper")
    parser.add_argument("--hub", required=True, help="the workbench directory (.workbench)")
    parser.add_argument("--label", required=True, help="names the log and the session, e.g. the head's short sha")
    parser.add_argument("--to", default="claude", help="the mailbox the result goes to (default claude)")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="-- then the command and its arguments")
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not LABEL_RE.fullmatch(args.label):
        parser.error(f"--label must match {LABEL_RE.pattern} (got {args.label!r})")
    if not command:
        parser.error("no command: pass it after --")
    hub = Path(args.hub)
    log = log_path(hub, args.label)
    print(f"suite {args.label}: {subprocess.list2cmdline(command)}", flush=True)
    code, failures = None, None
    try:
        code, text = run(command, log, echo_to_pane)
        failures = count_failures(text)
    except Exception as err:  # noqa: BLE001 - the result must still be mailed and the marker written
        text = f"run_helper: {type(err).__name__}: {err}"
        print(text, flush=True)
    subject = result_subject(args.label, code, failures)
    print(f"\n{subject}; log: {log}", flush=True)
    try:
        mid = post_result(hub, args.to, subject, command, code, failures, log, text)
        print(f"result mailed to {args.to} ({mid})", flush=True)
    except Exception as err:  # noqa: BLE001 - a lost mail is reported on screen, never raised
        print(f"run_helper: could not mail the result to {args.to}: {err}", flush=True)
    # The very last act: nothing may be printed after it, or the close cannot prove the pane untouched.
    sys.stdout.flush()
    import helper_done
    helper_done.write_marker(hub, "suite", exit=code, failures=failures)
    return code if isinstance(code, int) and 0 <= code < 256 else 1


if __name__ == "__main__":
    raise SystemExit(main())
