"""The agwintermctl dialect, spoken on top of agterm (Linux and macOS) - #60.

agworkbench drives its terminal through agwinterm's control verbs: `lib/agw.py` sends wire requests
(`session.new`, `session.type`, ...) and `Workbench.ps1` runs the `agwintermctl` CLI. agterm has
the same model - workspaces, sessions, a session split into two panes - behind `agtermctl`, but it
addresses panes differently and replies in its own shapes. This module is the one place that
translates, so the rest of the workbench keeps speaking agwinterm on every platform.

Two differences decide the shape of it:

- **Pane ids.** agwinterm gives every pane a GUID. agterm addresses a pane as (session, slot), the
  slot being `left` or `right`. Here the left pane's id is the session's own id - exactly as an
  unsplit agwinterm session is its own single pane - and the right pane's id is a uuid5 derived
  from the session id, so any process can compute it again from `AGTERM_SESSION_ID` and
  `AGTERM_PANE` with no state kept anywhere.
- **Case.** agwinterm's ids are lowercase GUIDs and the workbench checks that spelling (relay.py's
  pane-id rule); agterm's are uppercase. Ids leave here lowercase and are matched in any case.
- **Realization.** agterm starts a session's process only once the session has been shown. A
  session created with `--no-select` sits "not realized" and its command never runs, where
  agwinterm runs it in the background. So a background `session.new` here creates the session
  selected, which starts it, and then hands focus back to whatever was active before.

Run as a script it is an `agwintermctl` stand-in for the verbs the workbench uses (`lib/agwintermctl`
is the executable wrapper `Workbench.ps1` finds): plain output by default, the `{ok, result}`
envelope with `--json`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from typing import Any

# Fixed namespace for right-pane ids. Changing it orphans every recorded right pane.
RIGHT_PANE_NS = uuid.UUID("5d0f3c1e-7a52-4c4b-9e57-a6c1f0d4b8e2")


class CtlError(RuntimeError):
    """agterm refused the request, or could not be reached at all."""


# --- ids ------------------------------------------------------------------------------------

def right_pane_id(session_id: str) -> str:
    return str(uuid.uuid5(RIGHT_PANE_NS, session_id.upper()))


def pane_id(session_id: str, slot: str) -> str:
    return session_id.lower() if slot == "left" else right_pane_id(session_id)


def caller_pane() -> str | None:
    """This process's own pane id, from the variables agterm puts in every pane's environment."""
    session = os.environ.get("AGTERM_SESSION_ID")
    if not session:
        return None
    slot = "right" if os.environ.get("AGTERM_PANE") == "right" else "left"
    return pane_id(session, slot)


def bridge_env(env=None) -> None:
    """Give a process in an agterm pane the AGWINTERM_* variables the workbench reads, derived from
    agterm's own. No-op outside agterm, and never overrides values agwinterm itself set."""
    env = os.environ if env is None else env
    session = env.get("AGTERM_SESSION_ID")
    if not session or env.get("AGWINTERM_SESSION_ID"):
        return
    slot = "right" if env.get("AGTERM_PANE") == "right" else "left"
    env["AGWINTERM_ENABLED"] = "1"
    env["AGWINTERM_SESSION_ID"] = session.lower()
    env["AGWINTERM_PANE_ID"] = pane_id(session, slot)
    if env.get("AGTERM_WINDOW_ID"):
        env["AGWINTERM_WINDOW_ID"] = env["AGTERM_WINDOW_ID"]


# --- transport ------------------------------------------------------------------------------

def ctl_path() -> str:
    explicit = os.environ.get("AGTERMCTL")
    if explicit:
        return explicit
    found = shutil.which("agtermctl")
    if found:
        return found
    for candidate in ("/usr/bin/agtermctl", "/opt/agterm-linux/bin/agtermctl",
                      "/Applications/agterm.app/Contents/MacOS/agtermctl"):
        if os.path.isfile(candidate):
            return candidate
    raise CtlError("agtermctl not found: install agterm, or set AGTERMCTL to its full path")


def agtermctl(*args: str, stdin: str | None = None, timeout: float = 30.0) -> Any:
    """Run one agtermctl verb with --json and return its result. Raises CtlError on a refusal."""
    argv = [ctl_path(), *args, "--json"]
    socket = os.environ.get("AGT_SOCKET")
    if socket and "--socket" not in args:
        argv += ["--socket", socket]
    try:
        done = subprocess.run(argv, input=stdin.encode("utf-8") if stdin is not None else None,
                              capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as err:
        raise CtlError(f"agtermctl timed out: {' '.join(args[:2])}") from err
    except OSError as err:
        raise CtlError(f"agtermctl could not run: {err}") from err
    out = done.stdout.decode("utf-8", errors="replace").strip()
    try:
        envelope = json.loads(out) if out else None
    except json.JSONDecodeError:
        envelope = None
    if not isinstance(envelope, dict):
        detail = done.stderr.decode("utf-8", errors="replace").strip() or out or f"exit {done.returncode}"
        raise CtlError(f"agtermctl {' '.join(args[:2])}: {detail}")
    if not envelope.get("ok"):
        raise CtlError(str(envelope.get("error", "unknown error")))
    return envelope.get("result")


# --- the tree, in agwinterm's shape ---------------------------------------------------------

def raw_tree() -> dict[str, Any]:
    result = agtermctl("tree")
    tree = result.get("tree") if isinstance(result, dict) else None
    if not isinstance(tree, dict):
        raise CtlError("agtermctl returned an invalid session tree")
    return tree


def _session_node(session: dict[str, Any]) -> dict[str, Any]:
    sid = str(session["id"]).lower()
    split = bool(session.get("split") or session.get("hasSplit"))
    panes = [sid, right_pane_id(sid)] if split else [sid]
    node: dict[str, Any] = {
        "id": sid,
        "name": session.get("name", ""),
        "cwd": session.get("cwd", ""),
        "title": session.get("title", ""),
        "paneIds": panes,
        "focusedPane": panes[-1] if split and session.get("splitFocused") else panes[0],
        "active": bool(session.get("active")),
        "realized": bool(session.get("realized", True)),
    }
    if split:
        node["axis"] = session.get("splitAxis") or "vertical"
        node["paneCwds"] = [session.get("cwd", ""), session.get("splitCwd", session.get("cwd", ""))]
    for key in ("context", "status", "statusChangedAt"):
        if key in session:
            node[key] = session[key]
    restore: dict[str, str] = {}
    if session.get("restoreCommand"):
        restore[panes[0]] = session["restoreCommand"]
    if split and session.get("splitRestoreCommand"):
        restore[panes[1]] = session["splitRestoreCommand"]
    if restore:
        node["restoreCommands"] = restore
    return node


def tree() -> dict[str, Any]:
    raw = raw_tree()
    return {
        "workspaces": [
            {"id": str(ws["id"]).lower(), "name": ws.get("name", ""), "active": bool(ws.get("active")),
             "sessions": [_session_node(s) for s in ws.get("sessions", [])]}
            for ws in raw.get("workspaces", [])
        ]
    }


def _active_session(raw: dict[str, Any]) -> str | None:
    for ws in raw.get("workspaces", []):
        if ws.get("active"):
            for session in ws.get("sessions", []):
                if session.get("active"):
                    return str(session["id"])
    return None


def resolve(target: str | None, raw: dict[str, Any] | None = None) -> tuple[str, str]:
    """(session id, slot) for an agwinterm pane target: a pane id, a session id, a session name or
    `active`. No target means the caller's own pane, as agwinterm defaults to AGWINTERM_SESSION_ID."""
    if not target:
        target = caller_pane() or "active"
    raw = raw if raw is not None else raw_tree()
    sessions = [s for ws in raw.get("workspaces", []) for s in ws.get("sessions", [])]
    if target == "active":
        sid = _active_session(raw)
        if not sid:
            raise CtlError("no active session")
        session = next(s for s in sessions if s["id"] == sid)
        return sid, "right" if session.get("split") and session.get("splitFocused") else "left"
    wanted = target.lower()
    for session in sessions:
        sid = str(session["id"]).lower()
        if wanted == sid:
            return session["id"], "left"
        if wanted == right_pane_id(sid):
            return session["id"], "right"
    named = [s for s in sessions if s.get("name") == target]
    if len(named) == 1:
        session = named[0]
        return session["id"], "right" if session.get("split") and session.get("splitFocused") else "left"
    raise CtlError(f"no pane or session '{target}'")


def _real_workspace(workspace_id: str, raw: dict[str, Any]) -> str:
    """agterm's own spelling of a workspace id handed out here in lowercase."""
    for ws in raw.get("workspaces", []):
        if str(ws["id"]).lower() == workspace_id.lower():
            return ws["id"]
    return workspace_id


def _workspace_of(session_id: str, raw: dict[str, Any]) -> str | None:
    for ws in raw.get("workspaces", []):
        if any(s["id"].lower() == session_id.lower() for s in ws.get("sessions", [])):
            return ws["id"]
    return None


# --- verbs ----------------------------------------------------------------------------------

def _session_new(args: dict[str, Any]) -> str:
    raw = raw_tree()
    previous = _active_session(raw)
    argv = ["session", "new"]
    if args.get("name"):
        argv += ["--name", str(args["name"])]
    argv += ["--cwd", str(args.get("cwd") or os.getcwd())]
    if args.get("workspace"):
        argv += ["--workspace", _real_workspace(str(args["workspace"]), raw)]
    elif args.get("workspace-name"):
        argv += ["--workspace-name", str(args["workspace-name"])]
        if args.get("create-workspace"):
            argv.append("--create-workspace")
    else:
        # agwinterm puts a session beside its caller, not in whatever workspace was last clicked.
        mine = os.environ.get("AGTERM_SESSION_ID")
        workspace = _workspace_of(mine, raw) if mine else None
        if workspace:
            argv += ["--workspace", workspace]
    if args.get("command"):
        argv += ["--command", str(args["command"])]
    # Never --no-select: an unselected agterm session is not realized and its command never runs.
    result = agtermctl(*argv)
    sid = str(result.get("id") if isinstance(result, dict) else result).lower()
    if args.get("no-select") and previous:
        time.sleep(0.4)  # let the new surface spawn before it is hidden again
        try:
            agtermctl("session", "select", "--target", previous)
        except CtlError:
            pass
    return sid


def _session_split(args: dict[str, Any], target: str | None) -> str:
    sid, _ = resolve(target)
    mode = str(args.get("mode") or "toggle")
    argv = ["session", "split", mode, "--target", sid]
    if args.get("axis"):
        argv += ["--axis", str(args["axis"])]
    agtermctl(*argv)
    session = next((s for ws in raw_tree()["workspaces"] for s in ws["sessions"] if s["id"] == sid), None)
    if session and (session.get("split") or session.get("hasSplit")):
        # A fresh split pane is realized only while its session is on screen.
        if not session.get("realized", True) or not session.get("active"):
            _realize(sid)
        return right_pane_id(sid) if mode != "off" else sid.lower()
    return sid.lower()


def _realize(session_id: str) -> None:
    """Show a session once so agterm starts its processes, then put focus back."""
    previous = _active_session(raw_tree())
    agtermctl("session", "select", "--target", session_id)
    time.sleep(0.4)
    if previous and previous != session_id:
        try:
            agtermctl("session", "select", "--target", previous)
        except CtlError:
            pass


def realize_all() -> list[str]:
    """Start every session agterm restored but has not shown yet - after a restart in rerun mode,
    queue conductors and relays would otherwise sit dead until someone clicked on them."""
    raw = raw_tree()
    previous = _active_session(raw)
    started = []
    for ws in raw.get("workspaces", []):
        for session in ws.get("sessions", []):
            if not session.get("realized", True):
                agtermctl("session", "select", "--target", session["id"])
                time.sleep(0.4)
                started.append(session["id"])
    if previous and started:
        agtermctl("session", "select", "--target", previous)
    return started


def _type(sid: str, slot: str, text: str) -> None:
    """Type like agwinterm does: a newline is Enter. agterm delivers one injection containing a
    newline as a paste, which a TUI such as Claude Code keeps in its composer instead of
    submitting, so each line goes in on its own and every newline is a separate Enter."""
    lines = text.replace("\r\n", "\n").split("\n")
    for i, line in enumerate(lines):
        if line:
            agtermctl("session", "type", "--stdin", "--target", sid, "--pane", slot, stdin=line)
        if i < len(lines) - 1:
            time.sleep(0.05)  # let the TUI take the text before the key that submits it
            agtermctl("session", "type", "--stdin", "--target", sid, "--pane", slot, stdin="\r")


def _pane_args(target: str | None) -> list[str]:
    sid, slot = resolve(target)
    return ["--target", sid, "--pane", slot]


def request(cmd: str, *, target: str | None = None, args: dict[str, Any] | None = None) -> Any:
    """One agwinterm wire request, carried out on agterm. Returns the agwinterm-shaped result."""
    args = args or {}
    if cmd in ("ping", "version"):
        result = agtermctl("version")
        version = result.get("app", {}).get("version") if isinstance(result, dict) else result
        return f"agterm {version or ''}".strip()
    if cmd == "tree":
        return tree()
    if cmd == "session.new":
        return _session_new(args)
    if cmd == "session.split":
        return _session_split(args, target)
    if cmd in ("session.type", "session.write", "session.paste"):
        sid, slot = resolve(target)
        try:
            _type(sid, slot, str(args.get("text", "")))
        except CtlError as err:
            if "not realized" not in str(err):
                raise
            _realize(sid)
            _type(sid, slot, str(args.get("text", "")))
        return "typed"
    if cmd == "session.text":
        argv = ["session", "text", *_pane_args(target)]
        if args.get("all"):
            argv.append("--all")
        result = agtermctl(*argv, timeout=15.0)
        return result.get("text", "") if isinstance(result, dict) else str(result or "")
    if cmd == "session.restore":
        sid, slot = resolve(target)
        command = str(args.get("command", ""))
        pane = pane_id(sid, slot)
        if command in ("", "none"):
            agtermctl("session", "restore", "--clear", "--target", sid, "--pane", slot)
            return {"action": "cleared", "pane": pane, "session": sid.lower()}
        agtermctl("session", "restore", command, "--target", sid, "--pane", slot)
        return {"action": "pinned", "pane": pane, "session": sid.lower(), "command": command}
    if cmd == "session.close":
        sid, _ = resolve(target)
        agtermctl("session", "close", "--target", sid)
        return "closed"
    if cmd == "session.rename":
        sid, _ = resolve(target)
        agtermctl("session", "rename", str(args.get("name", "")), "--target", sid)
        return "renamed"
    if cmd == "session.context":
        sid, _ = resolve(target)
        if args.get("clear"):
            agtermctl("session", "context", "--clear", "--target", sid)
        else:
            agtermctl("session", "context", str(args.get("text", "")), "--target", sid)
        return {"session": sid.lower(), "context": args.get("text")}
    if cmd == "session.move":
        sid, _ = resolve(target)
        agtermctl("session", "move", _real_workspace(str(args["workspace"]), raw_tree()), "--target", sid)
        return "moved"
    if cmd == "session.select":
        sid, _ = resolve(target)
        agtermctl("session", "select", "--target", sid)
        return "selected"
    if cmd == "session.focus":
        sid, _ = resolve(target)
        slot = str(args.get("pane") or "primary")
        agtermctl("session", "focus", slot, "--target", sid)
        return "focused"
    if cmd == "session.status":
        sid, slot = resolve(target)
        argv = ["session", "status", str(args.get("status", "idle")), "--target", sid, "--pane", slot]
        if args.get("blink"):
            argv.append("--blink")
        if args.get("sound"):
            argv += ["--sound", "default"]
        agtermctl(*argv)
        return "ok"
    if cmd == "session.metrics":
        # agterm reports no grid size; the visible text's widest row stands in for it. Launches only
        # ask "is the window usable", and a shown agterm window always is.
        sid, slot = resolve(target)
        text = agtermctl("session", "text", "--target", sid, "--pane", slot, timeout=15.0)
        text = text.get("text", "") if isinstance(text, dict) else str(text or "")
        rows = text.split("\n")
        return {"cols": max([len(r) for r in rows] + [80]), "rows": len(rows)}
    if cmd == "surface.cursor":
        sid, slot = resolve(target)
        value = agtermctl("surface", "cursor", "--target", f"surface:{sid}:{slot}")
        return value.get("column", value) if isinstance(value, dict) else value
    if cmd == "notify":
        argv = ["notify", str(args.get("body", ""))]
        if args.get("title"):
            argv += ["--title", str(args["title"])]
        if target:
            sid, _ = resolve(target)
            argv += ["--target", sid]
        agtermctl(*argv)
        return "notified"
    if cmd == "workspace.new":
        result = agtermctl("workspace", "new", str(args.get("name", "")))
        return str(result.get("id") if isinstance(result, dict) else result).lower()
    if cmd.startswith("install"):
        return "agterm: nothing to install"  # hooks/skill come from `agtermctl integration install`
    raise CtlError(f"verb not supported on agterm: {cmd}")


# --- the agwintermctl CLI -------------------------------------------------------------------

def _parse_cli(argv: list[str]) -> tuple[str, str | None, dict[str, Any], bool, str | None]:
    """agwintermctl argv -> (cmd, target, args, json, stdin text). Covers the verbs agworkbench uses."""
    want_json = "--json" in argv
    rest = [a for a in argv if a != "--json"]
    target = None
    stdin_text = None
    if "--target" in rest:
        i = rest.index("--target")
        target = rest[i + 1]
        del rest[i:i + 2]
    if "--timeout" in rest:
        i = rest.index("--timeout")
        del rest[i:i + 2]
    if not rest:
        raise CtlError("no verb")
    if rest[0] in ("ping", "tree", "version"):
        return rest[0], target, {}, want_json, None
    if rest[0] == "notify":
        args: dict[str, Any] = {"body": rest[1] if len(rest) > 1 else ""}
        if "--title" in rest:
            args["title"] = rest[rest.index("--title") + 1]
        return "notify", target, args, want_json, None
    if rest[0] == "install":
        return "install", target, {}, want_json, None
    area, verb, tail = rest[0], (rest[1] if len(rest) > 1 else ""), rest[2:]
    cmd = f"{area}.{verb}"
    args = {}
    flags = {"--no-select", "--create-workspace", "--select", "--stdin", "--blink", "--clear", "--all", "--wait"}
    positional: list[str] = []
    i = 0
    while i < len(tail):
        token = tail[i]
        if token in flags:
            args[token[2:]] = True
            i += 1
        elif token.startswith("--") and i + 1 < len(tail):
            args[token[2:]] = tail[i + 1]
            i += 2
        else:
            positional.append(token)
            i += 1
    if args.pop("stdin", False):
        stdin_text = sys.stdin.read()
    if cmd in ("session.type", "session.write", "session.paste"):
        args["text"] = stdin_text if stdin_text is not None else (positional[0] if positional else "")
        args.pop("select", None)
    elif cmd == "session.restore":
        args["command"] = positional[0] if positional else ("none" if args.get("clear") else "")
    elif cmd == "session.split":
        if positional and positional[0] == "visibility":
            positional = positional[1:]
        args["mode"] = positional[0] if positional else "toggle"
    elif cmd in ("session.close", "session.select", "session.metrics"):
        if positional and not target:
            target = positional[0]
    elif cmd == "session.rename":
        args["name"] = positional[0] if positional else ""
    elif cmd == "session.context":
        args["text"] = stdin_text if stdin_text is not None else (positional[0] if positional else "")
    elif cmd == "session.move":
        args["workspace"] = positional[0] if positional else ""
    elif cmd == "session.focus":
        args["pane"] = positional[0] if positional else "primary"
    elif cmd == "session.status":
        args["status"] = positional[0] if positional else "idle"
    elif cmd == "workspace.new":
        args["name"] = positional[0] if positional else ""
    return cmd, target, args, want_json, stdin_text


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["env"]:
        # KEY=VALUE lines of bridge_env(), for a PowerShell or shell entry point to import.
        bridged = dict(os.environ)
        bridge_env(bridged)
        for key in ("AGWINTERM_ENABLED", "AGWINTERM_SESSION_ID", "AGWINTERM_PANE_ID", "AGWINTERM_WINDOW_ID"):
            if bridged.get(key) and bridged.get(key) != os.environ.get(key):
                print(f"{key}={bridged[key]}")
        return 0
    if argv[:1] == ["realize"]:
        for sid in realize_all():
            print(sid)
        return 0
    want_json = "--json" in argv
    try:
        cmd, target, args, want_json, _ = _parse_cli(argv)
        result = request(cmd, target=target, args=args)
    except CtlError as err:
        if want_json:
            print(json.dumps({"ok": False, "error": str(err)}))
        else:
            print(f"error: {err}", file=sys.stderr)
        return 1
    if want_json:
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
    elif isinstance(result, (dict, list)):
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
