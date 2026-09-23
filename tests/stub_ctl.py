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
if args == ["tree", "--json"]:
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
    workspace = next((w for w in tree["workspaces"] if w["name"] == option("--workspace-name")), None)
    if workspace is None:
        workspace = {"name": option("--workspace-name"), "sessions": []}
        tree["workspaces"].append(workspace)
    workspace["sessions"].append({"id": session_id, "name": name})
    scenario.setdefault("text", {})[session_id] = "relay up:" if name.endswith(" relay") else "Claude running"
    finish(session_id)
elif args[:3] == ["session", "split", "on"]:
    session = next(s for s in sessions if s["id"] == option("--target"))
    session["paneIds"] = [session["id"], scenario["right_id"]]
    scenario.setdefault("text", {})[scenario["right_id"]] = "PS C:\\checkout> "
    scenario["split_pending"] = scenario.get("split_delay", 0)
    finish(scenario["right_id"])
elif args[:2] == ["session", "text"]:
    finish(scenario.get("text", {}).get(option("--target"), ""))
elif args[:2] == ["session", "type"]:
    target = option("--target")
    scenario.setdefault("text", {})[target] = "relay up:" if target == scenario["relay_id"] else "Ask Codex to do anything\ngpt-test"
    finish()
elif args[:2] in (["session", "select"], ["session", "focus"]):
    finish()
else:
    finish("unexpected ctl arguments: " + repr(args), 97, True)
