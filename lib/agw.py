# Vendored from the ai-hub tooling (MIT, same author). Kept close to the original on purpose:
# it is the tested half of this project - see tests/ there and here.
"""Thin helper around agwinterm's control API.

Windows/agwinterm analog of the agtermctl calls in umputun's cookbook/two-agent-chat recipe.

Two transports, in this order:

1. The control pipe (`\\\\.\\pipe\\%AGWINTERM_PIPE%`) directly. One newline-delimited JSON request,
   one JSON response, both UTF-8. This is the transport that matters: reading a pane means reading
   box-drawing glyphs and Codex's `>` prompt chevron, and those survive the pipe intact.
2. The `agwintermctl` binary, when the pipe cannot be opened. .NET writes its stdout in the
   console's code page - 437 here - which turns every glyph outside it into `?`, so the fallback
   flips the console to UTF-8 for the duration of the call and puts it back afterwards.
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Iterator

CANDIDATES = [
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "agwinterm" / "agwintermctl.exe",
    Path.home() / "source" / "agwinterm" / "src" / "Agwinterm.Ctl" / "bin" / "Release" / "net10.0-windows" / "agwintermctl.exe",
]
CP_UTF8 = 65001


class CtlError(RuntimeError):
    """agwinterm refused the request, or could not be reached at all."""


# --- transport ------------------------------------------------------------------------------

def pipe_name() -> str:
    return os.environ.get("AGWINTERM_PIPE") or "agwinterm"


def _open_pipe():
    """Open the control pipe, or None when it is not there. A separate function so a test can
    hand the transport a fake handle and exercise the deadline with no terminal running."""
    try:
        return open(rf"\\.\pipe\{pipe_name()}", "r+b", buffering=0)
    except OSError:
        return None


def _pipe_roundtrip(handle, payload: dict[str, Any]) -> bytes:
    """Write one request and read one line back. Runs on a worker thread so the caller can time
    out: a blocking read on a named pipe has no deadline of its own, so a terminal that accepted
    the connection and then stopped answering would hang the agent forever."""
    handle.write((json.dumps(payload) + "\n").encode("utf-8"))
    chunks: list[bytes] = []
    while True:
        chunk = handle.read1(65536) if hasattr(handle, "read1") else handle.read(65536)
        if not chunk:
            break
        chunks.append(chunk)
        if b"\n" in chunk:
            break
    return b"".join(chunks).split(b"\n", 1)[0]


def _run_roundtrip(handle, payload: dict[str, Any], out: dict[str, Any]) -> None:
    """Body of the worker thread. An exception here must reach the caller as data, not vanish."""
    try:
        out["raw"] = _pipe_roundtrip(handle, payload)
    except Exception as err:      # noqa: BLE001 - reported through `out`, re-raised by the caller
        out["error"] = err


def _via_pipe(payload: dict[str, Any], timeout: float) -> dict[str, Any] | None:
    """One request over the control pipe. None when the pipe cannot be opened at all."""
    handle = _open_pipe()
    if handle is None:
        return None
    # A plain daemon thread, not a pool worker: ThreadPoolExecutor joins its threads at
    # interpreter shutdown, so a read that never returns would still hold the process open after
    # the caller had given up. A daemon thread does not, which is the whole point of the deadline.
    result: dict[str, Any] = {}
    worker = threading.Thread(
        target=lambda: _run_roundtrip(handle, payload, result),
        daemon=True, name="agw-pipe",
    )
    try:
        worker.start()
        worker.join(timeout)
        if worker.is_alive():
            handle.close()          # what unblocks the read still sitting in the worker
            raise CtlError(f"control pipe did not answer within {timeout:g}s")
        if "error" in result:
            raise CtlError(f"control pipe write/read failed: {result['error']}")
        raw = result.get("raw", b"")
    except OSError as err:
        raise CtlError(f"control pipe write/read failed: {err}") from err
    finally:
        try:
            handle.close()
        except OSError:
            pass
    if not raw:
        raise CtlError("no response from the control pipe")
    try:
        envelope = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as err:
        # The CLI transport wraps this. The pipe used to let a ValueError escape, so a caller
        # catching CtlError saw a different exception depending on which transport answered.
        raise CtlError(f"control pipe returned non-JSON: {raw[:200]!r}") from err
    # None is reserved for an unavailable pipe, not a decoded JSON null.
    if not isinstance(envelope, Mapping):
        raise CtlError(f'control pipe returned a non-object envelope: {envelope!r}')
    return envelope


def ctl_path() -> str:
    """Resolve agwintermctl: $AGWINTERMCTL, then PATH, then the known install locations."""
    explicit = os.environ.get("AGWINTERMCTL")
    if explicit:
        return explicit
    found = shutil.which("agwintermctl")
    if found:
        return found
    for candidate in CANDIDATES:
        if candidate.is_file():
            return str(candidate)
    raise CtlError(
        "agwintermctl not found and the control pipe is unreachable. "
        "Run `agwintermctl install cli` once, or set AGWINTERMCTL to its full path."
    )


def _console_utf8():
    """Flip the console output code page to UTF-8 and hand back a restore callable."""
    try:
        kernel32 = ctypes.windll.kernel32
        previous = kernel32.GetConsoleOutputCP()
    except Exception:  # not Windows, or no console
        return lambda: None
    if not previous or previous == CP_UTF8:
        return lambda: None
    kernel32.SetConsoleOutputCP(CP_UTF8)
    return lambda: kernel32.SetConsoleOutputCP(previous)


def _via_cli(payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    """Fallback: hand the same request to agwintermctl and read its raw JSON envelope."""
    args = [ctl_path()] + _cli_args(payload) + ["--json"]
    restore = _console_utf8()
    try:
        done = subprocess.run(args, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as err:
        raise CtlError(f"agwintermctl timed out: {payload.get('cmd')}") from err
    finally:
        restore()
    out = (done.stdout or b"").decode("utf-8", errors="replace").strip()
    if done.returncode != 0 and not out:
        detail = (done.stderr or b"").decode("utf-8", errors="replace").strip() or f"exit {done.returncode}"
        raise CtlError(f"agwintermctl {payload.get('cmd')}: {detail}")
    try:
        return json.loads(out)
    except json.JSONDecodeError as err:
        raise CtlError(f"agwintermctl returned non-JSON: {out[:200]!r}") from err


def _cli_args(payload: dict[str, Any]) -> list[str]:
    """Map a control request back onto the CLI verbs, for the fallback transport."""
    cmd = payload["cmd"]
    args_in = payload.get("args") or {}
    target = payload.get("target")
    area, _, verb = cmd.partition(".")
    out = [area] + ([verb] if verb else [])
    if cmd in ("session.type", "session.write", "session.paste"):
        out += ["--select", str(args_in.get("text", ""))]
    elif cmd == 'session.new':
        for key, value in args_in.items():
            if isinstance(value, bool):
                if value:
                    out.append('--' + key)
            else:
                out += ['--' + key, str(value)]
    elif cmd == 'session.restore':
        out += [str(args_in.get('command', ''))]
    elif cmd == 'session.close':
        # `session close [target]`: the target is positional here, not --target.
        return out + ([target] if target else [])
    elif cmd == "session.status":
        out += [str(args_in.get("status", "idle"))]
        if args_in.get("blink"):
            out.append("--blink")
        if args_in.get("sound"):
            out.append("--sound")
    elif cmd == "notify":
        out = ["notify", str(args_in.get("body", ""))]
        if args_in.get("title"):
            out += ["--title", str(args_in["title"])]
    if target:
        out += ["--target", target]
    return out


def request(cmd: str, *, target: str | None = None, args: dict[str, Any] | None = None,
            timeout: float = 10.0) -> Any:
    """Send one control request and return its result. Raises CtlError on a refusal."""
    payload: dict[str, Any] = {"cmd": cmd}
    if target:
        payload["target"] = target
    if args:
        payload["args"] = args
    envelope = _via_pipe(payload, timeout)
    if envelope is None:
        envelope = _via_cli(payload, timeout)
    if not isinstance(envelope, Mapping):
        raise CtlError(f'control request returned a non-object envelope: {envelope!r}')
    if not envelope.get("ok"):
        raise CtlError(str(envelope.get("error", "unknown error")))
    return envelope.get("result")


# --- the bits everything else uses ----------------------------------------------------------

def running() -> bool:
    try:
        request("ping", timeout=4.0)
        return True
    except (CtlError, OSError):
        return False


def version() -> str:
    return str(request("ping"))


def tree() -> dict[str, Any]:
    return request("tree")


def sessions(snapshot: dict[str, Any] | None = None) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
    """Yield (workspace, session) for every session in the tree."""
    snapshot = snapshot if snapshot is not None else tree()
    for workspace in snapshot.get("workspaces", []):
        for session in workspace.get("sessions", []):
            yield workspace, session


def panes_of(session: dict[str, Any]) -> list[str]:
    """Pane ids of a session, left to right. An unsplit session is its own single pane."""
    ids = session.get("paneIds")
    return list(ids) if ids else [session["id"]]


def find_session(session_id: str, snapshot: dict[str, Any] | None = None):
    for workspace, session in sessions(snapshot):
        if session["id"] == session_id:
            return workspace, session
    return None


def find_pane(pane_id: str, snapshot: dict[str, Any] | None = None):
    """Locate a pane anywhere in the tree: (workspace, session, index-within-session)."""
    for workspace, session in sessions(snapshot):
        panes = panes_of(session)
        if pane_id in panes:
            return workspace, session, panes.index(pane_id)
    return None


def my_pane() -> str | None:
    """This process's own pane. AGWINTERM_PANE_ID is per-pane, so a split never collides."""
    return os.environ.get("AGWINTERM_PANE_ID") or os.environ.get("AGWINTERM_SESSION_ID") or None


def pane_text(pane_id: str) -> str:
    """The pane's visible buffer as plain text."""
    return str(request("session.text", target=pane_id, timeout=15.0))


def cursor_column(pane_id: str) -> int:
    """Read the zero-based caret column; it does not prove an empty composer."""
    value = request('surface.cursor', target=pane_id)
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str):
        value = value.strip()
        if value.isascii() and value.isdecimal():
            try:
                return int(value)
            except ValueError:
                pass  # Python can reject decimal strings exceeding its digit limit.
    raise CtlError(f'surface.cursor returned an invalid column: {value!r}')


def type_into(pane_id: str, text: str) -> None:
    """Type keystrokes into a pane. A newline is Enter; a tab is Tab."""
    request("session.type", target=pane_id, args={"text": text})


def close_session(session_id: str) -> None:
    """Close a whole session (every pane in it). Callers prove the panes may be closed first."""
    request("session.close", target=session_id)


def clear_restore(pane_id: str) -> None:
    """Remove a pane's pinned restart command, so a closed session is not revived on restart."""
    request("session.restore", target=pane_id, args={"command": "none"})


def notify(pane_id: str, message: str, title: str | None = None) -> None:
    args: dict[str, Any] = {"body": message}
    if title:
        args["title"] = title
    request("notify", target=pane_id, args=args)


def set_status(state: str, *, sound: bool = False, blink: bool = False, pane_id: str | None = None) -> None:
    args: dict[str, Any] = {"status": state}
    if sound:
        args["sound"] = "true"
    if blink:
        args["blink"] = True
    request("session.status", target=pane_id or (my_pane() or "active"), args=args)
