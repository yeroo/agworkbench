"""Stateful, terminal-free agwintermctl fixture; never executes a supplied command."""

import json
import os
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
scenario_path = Path(os.environ["STUB_CTL_SCENARIO"])
scenario = json.loads(scenario_path.read_text(encoding="utf-8"))
args = sys.argv[1:]
with Path(os.environ["STUB_CTL_CALLS"]).open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(args) + "\n")


def finish(output="", code=0, stderr=False):
    scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
    if output:
        print(output, file=sys.stderr if stderr else sys.stdout)
    raise SystemExit(code)


def option(name):
    return args[args.index(name) + 1]


for response in scenario.get("responses", []):
    if not response.get("used") and re.search(response["args"], " ".join(args)):
        response["used"] = response.get("once", False)
        finish(response.get("stdout", ""), response.get("exit", 0), response.get("stderr", False))

tree = scenario.setdefault("tree", {"workspaces": []})
sessions = [s for w in tree["workspaces"] for s in w["sessions"]]
if args == ['config', 'get', 'restore-commands', '--json']:
    finish(json.dumps({'ok': True, 'result': scenario.get('restore_enabled', 'true')}))
elif args == ['config', 'set', 'restore-commands', 'true']:
    if not scenario.get('ignore_restore_set'):
        scenario['restore_enabled'] = 'true'
    finish('restore-commands = true  (applies to new sessions)')
elif args[:2] == ['session', 'restore']:
    pane = option('--target')
    session = next(s for s in sessions if pane in s.get('paneIds', [s['id']]))
    session.setdefault('restoreCommands', {})[pane] = args[2]
    finish(json.dumps({'action': 'pinned', 'pane': pane, 'session': session['id'], 'command': args[2]}))
elif args == ["tree", "--json"]:
    if scenario.pop('fail_next_tree', False):
        finish('tree failed after split reply', 1)
    snapshot = json.loads(json.dumps(tree))
    if scenario.get("split_pending", 0):
        scenario["split_pending"] -= 1
        for workspace in snapshot["workspaces"]:
            for session in workspace["sessions"]:
                if session["id"] == scenario["main_id"]:
                    session.pop("paneIds", None)
    finish(json.dumps({"ok": True, "result": snapshot}))
elif args[:2] == ["session", "new"]:
    name = option("--name")
    session_id = scenario["relay_id"] if name.endswith(" relay") else scenario["main_id"]
    if not name.endswith(' relay'):
        identity = Path(option('--cwd')) / '.workbench/state/claude.json'
        scenario.setdefault('created_claude_identities', []).append(
            json.loads(identity.read_text(encoding='utf-8-sig')) if identity.exists() else None)
    workspace = next((w for w in tree["workspaces"] if w["name"] == option("--workspace-name")), None)
    if workspace is None:
        workspace = {"name": option("--workspace-name"), "sessions": []}
        tree["workspaces"].append(workspace)
    workspace["sessions"].append({"id": session_id, "name": name})
    scenario.setdefault("text", {})[session_id] = "relay up:" if name.endswith(" relay") else "Claude running"
    finish(session_id)
elif args[:2] == ["workspace", "new"]:
    workspace_id = scenario['workspace_id']
    tree['workspaces'].append({'id': workspace_id, 'name': args[2], 'sessions': []})
    finish(workspace_id)
elif args[:2] == ["session", "rename"]:
    session = next(s for s in sessions if s['id'] == option('--target'))
    session['name'] = args[2]
    finish(json.dumps({'session': session['id'], 'name': session['name']}))
elif args[:2] == ["session", "move"]:
    session = next(s for s in sessions if s['id'] == option('--target'))
    destination = next(w for w in tree['workspaces'] if w.get('id') == args[2])
    for workspace in tree['workspaces']:
        workspace['sessions'] = [s for s in workspace['sessions'] if s['id'] != session['id']]
    destination['sessions'].append(session)
    if scenario.pop('fail_move_discovery', False):
        scenario['fail_next_tree'] = True
    finish('moved')
elif args[:3] == ["session", "split", "on"]:
    session = next(s for s in sessions if s["id"] == option("--target"))
    session["paneIds"] = [session.get('paneIds', [session['id']])[0], scenario["right_id"]]
    scenario.setdefault("text", {})[scenario["right_id"]] = "PS C:\\checkout> "
    scenario["split_pending"] = scenario.get("split_delay", 0)
    if scenario.pop('fail_split_confirmation', False):
        scenario['fail_next_tree'] = True
    finish(scenario["right_id"])
elif args[:2] == ["session", "text"]:
    if option("--target") == scenario["relay_id"] and scenario.get("stop_file"):
        if Path(scenario["stop_file"]).exists():
            scenario["stop_seen"] = True
            if not scenario.get("ignore_stop"):
                scenario.setdefault("text", {})[scenario["relay_id"]] = "PS C:\\relay> "
    finish(scenario.get("text", {}).get(option("--target"), ""))
elif args[:2] == ["session", "type"]:
    target = option("--target")
    scenario.setdefault('successful_types', []).append(target)
    if target == scenario["relay_id"]:
        if scenario.get("stop_file") and Path(scenario["stop_file"]).exists():
            finish("relay stop file must be cleared before restarting", 98, True)
        text = "relay up:"
    elif "pane-claude.ps1" in option("--select"):
        text = "Claude running\nbypass permissions on"
    else:
        text = "Ask Codex to do anything\ngpt-test"
    scenario.setdefault("text", {})[target] = text
    finish()
elif args[:2] in (["session", "select"], ["session", "focus"]):
    finish()
else:
    finish("unexpected ctl arguments: " + repr(args), 97, True)
