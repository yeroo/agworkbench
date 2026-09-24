# Vendored from the ai-hub tooling (MIT, same author). Kept close to the original on purpose:
# it is the tested half of this project - see tests/ there and here.
"""The mailbox: agent registry + file-backed inbox, rooted at AI_HUB (the workbench's .workbench/).

Two agents that can type into each other's panes still need somewhere durable to put anything
longer than one line - a review, a diff, a list of findings. The pane is the doorbell; this is
the mailbox. Messages are plain markdown files with a small frontmatter block, so any agent (or
you) can read one with `cat` and nothing is locked behind a running process.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

def hub_root() -> Path:
    """Where the hub lives. `AI_HUB` overrides it, so a test can point the registry and the
    mailbox at a temp directory instead of the real one."""
    override = os.environ.get("AI_HUB")
    return Path(override).expanduser().resolve() if override else Path(__file__).resolve().parent.parent


def reload_paths() -> None:
    """Recompute the module paths from the environment. Call after changing `AI_HUB`."""
    global HUB, INBOX, STATE, REGISTRY, LOG
    HUB = hub_root()
    INBOX = HUB / "inbox"
    STATE = HUB / "state"
    REGISTRY = STATE / "agents.json"
    LOG = STATE / "log.jsonl"


HUB = INBOX = STATE = REGISTRY = LOG = Path()
reload_paths()

KINDS = ("message", "task", "question", "answer", "review-request", "review", "handoff", "note")
BOX_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,40}$")
TOOLS = ("claude", "codex", "other")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def new_id(sender: str) -> str:
    return f"{stamp()}-{sender}-{secrets.token_hex(2)}"


# --- registry -------------------------------------------------------------------------------

def load_registry() -> dict[str, Any]:
    if not REGISTRY.is_file():
        return {"agents": {}}
    try:
        data = json.loads(REGISTRY.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"agents": {}}
    data.setdefault("agents", {})
    return data


def save_registry(data: dict[str, Any]) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = REGISTRY.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, REGISTRY)


def detect_tool() -> str:
    """Which agent is running this process, from the env each one sets."""
    if os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CODE_SESSION_ID"):
        return "claude"
    if os.environ.get("CODEX_SANDBOX") or os.environ.get("CODEX_HOME") or os.environ.get("CODEX_THREAD_ID"):
        return "codex"
    agent = (os.environ.get("AI_AGENT") or "").lower()
    for tool in ("claude", "codex"):
        if tool in agent:
            return tool
    return "other"


def slug(text: str) -> str:
    out = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return out or "agent"


def default_box(tool: str | None = None, cwd: str | None = None) -> str:
    tool = tool or detect_tool()
    where = Path(cwd or os.getcwd()).name
    return f"{tool}-{slug(where)}"


def whoami() -> dict[str, Any] | None:
    """This process's own registry entry, matched on AI_BOX first, then pane id."""
    registry = load_registry()["agents"]
    pane = os.environ.get("AGWINTERM_PANE_ID") or os.environ.get("AGWINTERM_SESSION_ID")
    named = os.environ.get("AI_BOX")
    if named and named in registry:
        return registry[named]
    if pane:
        for entry in registry.values():
            if entry.get("pane") == pane:
                return entry
    return None


def register(box: str | None = None, *, tool: str | None = None, role: str | None = None,
             note: str | None = None, cwd: str | None = None, pane: str | None = None) -> dict[str, Any]:
    """Claim a box. With `pane` it claims one FOR ANOTHER agent - an agent that started before
    the hub existed and so cannot register itself. That entry is marked `by: <registrar>` so it is
    obvious the agent did not say this about itself."""
    tool = tool or detect_tool()
    cwd = cwd or os.getcwd()
    existing = None if pane else whoami()
    box = box or (existing or {}).get("box") or os.environ.get("AI_BOX") or default_box(tool, cwd)
    if not BOX_RE.match(box):
        raise ValueError(f"bad box name {box!r}: use lowercase letters, digits, . _ -")
    data = load_registry()
    entry = dict(data["agents"].get(box, {}))
    entry.update({
        "box": box,
        "tool": tool,
        "cwd": cwd,
        "pane": pane or os.environ.get("AGWINTERM_PANE_ID") or os.environ.get("AGWINTERM_SESSION_ID"),
        # Only from the environment. Registering FOR another pane knows that pane's id and not
        # the session that holds it, and storing the pane id here would put one kind of id under a
        # key docs/protocol.md documents as another.
        "session": None if pane else os.environ.get("AGWINTERM_SESSION_ID"),
        "window": os.environ.get("AGWINTERM_WINDOW_ID"),
        "pid": None if pane else os.getpid(),
        "last_seen": now_iso(),
    })
    if pane:
        entry["by"] = (whoami() or {}).get("box") or default_box()
    else:
        entry.pop("by", None)
    entry.pop("closed_at", None)  # registering again is evidence the pane is alive
    entry.setdefault("registered", now_iso())
    if role:
        entry["role"] = role
    if note:
        entry["note"] = note
    # A pane hosts one agent: drop stale entries that claim the same pane under another name.
    if entry.get("pane"):
        for other, value in list(data["agents"].items()):
            if other != box and value.get("pane") == entry["pane"]:
                del data["agents"][other]
    data["agents"][box] = entry
    save_registry(data)
    ensure_box(box)
    return entry


def touch(box: str) -> None:
    data = load_registry()
    if box in data["agents"]:
        data["agents"][box]["last_seen"] = now_iso()
        save_registry(data)


def lookup(box: str) -> dict[str, Any] | None:
    return load_registry()["agents"].get(box)


def boxes() -> list[str]:
    return sorted(load_registry()["agents"])


def sweep(live_panes, *, prune: bool = False, dry_run: bool = False) -> dict[str, list[str]]:
    """Reconcile the registry against the panes that are actually open.

    An entry whose pane is gone is stamped `closed_at`, or deleted outright when `prune` is set.
    An entry with no pane recorded is left alone - it never claimed one, so its absence from the
    live set says nothing. An empty live set is treated the same way: a terminal that is not
    running is not evidence that a pane is gone, so nothing is touched.

    Returns the report as lists of box names under `marked`, `pruned`, `revived`, `live`,
    `no_pane`, `already_closed` and `unknown`.
    """
    live = set(live_panes or ())
    report: dict[str, list[str]] = {"marked": [], "pruned": [], "revived": [], "live": [],
                                    "no_pane": [], "already_closed": [], "unknown": []}
    data = load_registry()
    if not live:  # unknown, not gone
        for box, entry in data["agents"].items():
            report["no_pane" if not entry.get("pane") else "unknown"].append(box)
        for key in report:
            report[key].sort()
        return report
    changed = False
    for box, entry in list(data["agents"].items()):
        pane = entry.get("pane")
        if not pane:
            report["no_pane"].append(box)
            continue
        if pane in live:
            report["live"].append(box)
            if entry.pop("closed_at", None) is not None:
                report["revived"].append(box)
                changed = True
            continue
        if prune:
            report["pruned"].append(box)
            del data["agents"][box]
            changed = True
            continue
        if entry.get("closed_at"):
            report["already_closed"].append(box)
            continue
        entry["closed_at"] = now_iso()
        report["marked"].append(box)
        changed = True
    for key in report:
        report[key].sort()
    if changed and not dry_run:
        save_registry(data)
        append_log({"at": now_iso(), "event": "sweep", "marked": report["marked"],
                    "pruned": report["pruned"], "revived": report["revived"]})
    return report


# --- messages -------------------------------------------------------------------------------

def box_dir(box: str) -> Path:
    """The mailbox directory for a box. Validated HERE, not only in register(): every write path
    reaches the filesystem through this function, and `agmsg send --force --to ..` would otherwise
    put a file wherever the name pointed."""
    if not BOX_RE.match(box):
        raise ValueError(f"bad box name {box!r}: use lowercase letters, digits, . _ -")
    return INBOX / box


def ensure_box(box: str) -> Path:
    directory = box_dir(box)
    (directory / "read").mkdir(parents=True, exist_ok=True)
    (directory / "archive").mkdir(parents=True, exist_ok=True)
    return directory


def write_message(*, to: str, sender: str, subject: str, body: str, kind: str = "message",
                  thread: str | None = None, refs: list[str] | None = None,
                  message_id: str | None = None) -> Path:
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}: one of {', '.join(KINDS)}")
    if message_id is not None and not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._-]{0,199}', message_id):
        raise ValueError('bad message id')
    directory = ensure_box(to)
    message_id = message_id or new_id(sender)
    # Replaying an outbox must not overwrite or resurrect an already-read message.
    for folder in (directory, directory / 'read', directory / 'archive'):
        existing = folder / f'{message_id}.md'
        if existing.is_file():
            return existing
    head = [
        "---",
        f"id: {message_id}",
        f"from: {sender}",
        f"to: {to}",
        f"kind: {kind}",
        f"subject: {subject}",
        f"created: {now_iso()}",
    ]
    if thread:
        head.append(f"thread: {thread}")
    if refs:
        head.append("refs:")
        head += [f"  - {ref}" for ref in refs]
    head.append("---")
    path = box_dir(to) / f"{message_id}.md"
    # Publish a complete file without replacing an existing id, even on a concurrent replay.
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=directory,
                                     suffix='.tmp', delete=False) as handle:
        tmp = Path(handle.name)
        handle.write("\n".join(head) + "\n\n" + body.rstrip() + "\n")
    try:
        try:
            os.link(tmp, path)
        except FileExistsError:
            return path
    finally:
        tmp.unlink()
    append_log({"at": now_iso(), "event": "send", "id": message_id, "from": sender, "to": to,
                "kind": kind, "subject": subject})
    return path


def parse_message(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    meta: dict[str, Any] = {"path": str(path), "refs": []}
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            head, body = text[3:end], text[end + 4:]
            key = None
            for line in head.splitlines():
                if line.startswith("  - ") and key == "refs":
                    meta["refs"].append(line[4:].strip())
                    continue
                if ":" in line:
                    key, _, value = line.partition(":")
                    key, value = key.strip(), value.strip()
                    if key and key != "refs":
                        meta[key] = value
    meta["body"] = body.strip()
    meta.setdefault("id", path.stem)
    meta.setdefault("subject", "(no subject)")
    return meta


def find_message(box: str, message_id: str) -> Path | None:
    for folder in ("", "read", "archive"):
        directory = box_dir(box) / folder if folder else box_dir(box)
        if not directory.is_dir():
            continue
        exact = directory / f"{message_id}.md"
        if exact.is_file():
            return exact
        matches = sorted(p for p in directory.glob("*.md") if message_id in p.stem)
        if len(matches) == 1:
            return matches[0]
    return None


def unread(box: str) -> list[Path]:
    directory = box_dir(box)
    return sorted(p for p in directory.glob("*.md")) if directory.is_dir() else []


def mark_read(path: Path) -> Path:
    target = path.parent / "read" / path.name
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(path, target)
    return target


def append_log(record: dict[str, Any]) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


def age(iso: str | None) -> str:
    if not iso:
        return "-"
    try:
        seen = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return iso
    seconds = int(time.time() - seen.timestamp())
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    if seconds < 172800:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"
