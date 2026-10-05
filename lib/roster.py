#!/usr/bin/env python3
"""roster - the implementer roster (#109): the entries `-Implementer auto` chooses from.

`implementerRoster` in ~/.agworkbench.json is a list of {id, tool, model?, note}. The same rules live in
Workbench.ps1 (Get-RosterProblem, Get-FailoverOrderProblem); tests/fixtures/roster-cases.json is the case
table both implementations are tested against. A model naming "astra" is refused wherever it is read.

  python lib/roster.py check [--config PATH]     validate the config's roster; prints it as JSON
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
from pathlib import Path

TOOLS = ('codex', 'claude', 'kimi')
ID_RE = re.compile(r'[a-z0-9][a-z0-9-]*')
MODEL_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._\[\]-]*')       # the claudeImplementerModel rule
REFUSED_MODEL = re.compile(r'astra', re.I)
KEYS = ('id', 'tool', 'model', 'note')
NOTE_MAX = 200

DEFAULT_ROSTER = [
    {'id': 'claude-sonnet', 'tool': 'claude', 'model': 'claude-sonnet-5-5', 'note': 'default for most code'},
    {'id': 'claude-opus', 'tool': 'claude', 'model': 'claude-opus-5-5', 'note': 'hardest design/cross-cutting work'},
    {'id': 'kimi', 'tool': 'kimi', 'note': 'cheapest in Claude tokens; well-scoped issues'},
    {'id': 'codex-sol', 'tool': 'codex', 'model': 'gpt-6.1-sol', 'note': 'workhorse'},
    {'id': 'codex-luna', 'tool': 'codex', 'model': 'gpt-6-luna', 'note': 'small, mechanical, docs/tests-only changes'},
]


class RosterError(ValueError):
    pass


def config_path() -> Path:
    return Path(os.environ.get('AGWORKBENCH_CONFIG', Path.home() / '.agworkbench.json')).resolve()


def model_problem(model) -> str | None:
    if not isinstance(model, str) or not MODEL_RE.fullmatch(model):
        return f'a model must be a model name such as "claude-sonnet-5-5" (got {model!r})'
    if REFUSED_MODEL.search(model):
        return f"the model '{model}' is refused: gpt-6-astra is never used"
    return None


def problem(roster) -> str | None:
    """Why this roster is invalid, or None."""
    if not isinstance(roster, list):
        return 'must be a list of entries'
    if not roster:
        return 'must have at least one entry'
    seen = set()
    for entry in roster:
        if not isinstance(entry, dict):
            return f'entry {entry!r} must be an object'
        for key in entry:
            if key not in KEYS:
                return f"has an unknown key '{key}' (allowed: {', '.join(KEYS)})"
        ident = entry.get('id')
        if not isinstance(ident, str) or not ID_RE.fullmatch(ident):
            return f'entry id {ident!r} must match {ID_RE.pattern}'
        if ident in seen:
            return f"entry id '{ident}' is listed twice"
        seen.add(ident)
        if entry.get('tool') not in TOOLS:
            return f"entry '{ident}': tool must be codex, claude or kimi (got {entry.get('tool')!r})"
        if 'model' in entry:
            why = model_problem(entry['model'])
            if why:
                return f"entry '{ident}': {why}"
        note = entry.get('note')
        if not isinstance(note, str) or not note.strip() or len(note) > NOTE_MAX:
            return f"entry '{ident}': note must be a non-empty string of at most {NOTE_MAX} characters"
    return None


def load(settings) -> list[dict]:
    """The configured roster, or the default one; RosterError naming what is wrong."""
    roster = settings.get('implementerRoster') if isinstance(settings, dict) else None
    if roster is None:
        return copy.deepcopy(DEFAULT_ROSTER)
    why = problem(roster)
    if why:
        raise RosterError(f'implementerRoster: {why}')
    return copy.deepcopy(roster)


def entry_of(roster, ident):
    return next((e for e in roster if e['id'] == ident), None)


def failover_problem(order, roster) -> str | None:
    """Why this failoverOrder is invalid, or None: a list, no duplicates, every element a tool or a roster
    id, at least two distinct tools among them."""
    if not isinstance(order, list):
        return 'must be a list of tools or roster ids'
    tools = set()
    for item in order:
        if not isinstance(item, str):
            return f'must list tools or roster ids (got {item!r})'
        if item in TOOLS:
            tools.add(item)
            continue
        entry = entry_of(roster, item)
        if entry is None:
            return f"names '{item}', which is neither a tool (codex, claude, kimi) nor an implementerRoster id"
        tools.add(entry['tool'])
    if len(set(order)) != len(order):
        return f"must not list an element twice (got '{', '.join(order)}')"
    if len(tools) < 2:
        return f"must list at least two distinct tools of codex, claude and kimi (got '{', '.join(order)}')"
    return None


def resolve_order(order, roster) -> list[dict]:
    """failoverOrder as entries: a roster id is its entry (with its model); a tool name that is no roster id is
    the bare tool, `{'id': tool, 'tool': tool, 'bare': True}`, with no model: today's behaviour, so the default
    order leaves claudeImplementerModel and each tool's own default model in charge."""
    entries = []
    for item in order:
        entry = entry_of(roster, item)
        entries.append(dict(entry) if entry else {'id': item, 'tool': item, 'bare': True})
    return entries


def next_failover(order, roster, limited: str, recorded=()) -> dict | None:
    """The first element of failoverOrder whose tool is not the limited one and has no recorded limit: a
    limited claude-sonnet never fails over to claude-opus. The caller still checks the tool is usable."""
    for entry in resolve_order(order, roster):
        if entry['tool'] != limited and entry['tool'] not in recorded:
            return entry
    return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='roster')
    sub = parser.add_subparsers(dest='command', required=True)
    check = sub.add_parser('check')
    check.add_argument('--config')
    args = parser.parse_args(argv)
    path = Path(args.config) if args.config else config_path()
    try:
        settings = json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}
        print(json.dumps(load(settings), indent=2))
    except (OSError, ValueError) as err:
        print(f'roster: {err}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
