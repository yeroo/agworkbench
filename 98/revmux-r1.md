# Review: workbench / r1

scope: `C:\Users\boris\source\workbench\agworkbench-issue-98\.revmux\tasks\workbench\r1\input\scope.md`

## Major

### --dry-run still runs the no-pin alert and the budget give-up for real: mail, notify, blocked status, waiting.json, loop-state

`lib/relay.py:1002-1023`

`ExitWatch.restart` checks `self.relay.dry_run` only after the no-pin branch and the budget branch. Neither `alert()` nor `give_up()` checks dry run itself.

Failure case 1: a dry-run relay sees an exited pane with no `restoreCommands` pin. After the 2-minute grace period it calls `alert()`, which runs `agw.notify` on the planner pane and writes real `exit` mail to the planner's inbox through `hub.write_message`.

Failure case 2: a dry-run relay reads the real `state/relay.json`. If a live relay recorded 3 restarts within the hour, the dry run reaches `give_up()`. That sets the planner pane to blocked with sound, writes `state/waiting.json`, and in queue mode calls `conductor.write_loop_state(..., 'blocked', cause='environment')`.

The rest of the relay keeps dry runs passive:
- `StallWatch.escalate` logs `[dry-run] would report the loop blocked` and returns.
- `StallWatch.pointer` files no mail in dry run.
- `Relay.hold` does not alert in dry run.

AC6 and the plan say a dry run only logs `[dry-run] would restart ...`. `Guards.test_dry_run` covers only the case with a pin and budget left, so neither failure case is tested.

Fix: Check `self.relay.dry_run` right after `blocked_by`, before the pin and budget branches, log `[dry-run] would alert/give up ...` once and return. Or guard `alert` and `give_up` on dry_run the way `StallWatch.escalate` does.

_confidence: 99 | sources: bugs+impl, arch+quality, adversarial | lenses: bugs, impl, architecture, quality, adversarial | verdict: confirmed_

### After give_up the relay still restarts the pane later in the same episode, leaving waiting.json, the queue's blocked state and a false 'does not restart it again' alert in place

`lib/relay.py:1016-1089`

When 3 restarts are already in the window, `restart()` calls `give_up` once (guarded by the 'budget' note). On every later tick of the same episode it calls `recent()` again. Once the oldest restart leaves EXIT_WINDOW, the pin is typed. `blocked_by` does not consult waiting.json. `test_budget_three_per_hour` (tests/test_exit_restart.py:429-431) pins this: the pane stays exited from minute 30 and is restarted at about minute 62. The plan's 'an exit after the hour restarts again' suggests some of this is intended, but give_up has already:
- told the human and the planner 'the relay does not restart it again';
- set the planner pane to blocked;
- written state/waiting.json (by: relay);
- in queue mode, reported `loop-state blocked` with cause environment.

None of that is undone. The agent comes back while waiting.json still exempts the stall watch (relay.py:629), and the queue still shows the member blocked until someone runs `wb.py status active`. That contradicts the alert text and start-github-issue.md:271.

Fix: Make giving up final for the episode: in `restart()`, return early when 'budget' is in `episode['notes']`, before calling `recent()`. Only a new episode (pane back and exited again) should check the budget again. Update the test at lines 429-431 to expect no 4th restart in that episode.

_confidence: 75 | sources: bugs+impl, docs+tests | lenses: bugs, impl, docs, tests, comments | verdict: refined_

### idle_blockers' new early return turns a crashed Claude frame from idle into a hard blocker, so the close now refuses where it used to proceed

`lib/closer.py:712-714`

Before this change, a crashed Claude pane read as idle. Its frame (rules, an empty `>` composer, the footer, then `PS X:\...> ` below) parsed as an empty composer with no busy turn, so `idle_blockers` returned []. Now `ps_prompt_last` returns '... is at a shell prompt (its agent exited)' first, so for Claude panes this is more than a wording change.

`Closer.agent_blockers` adds `idle_blockers` to `hard` (closer.py:562), and `overdue_ok` never overrides a hard blocker (closer.py:323). `ExitWatch.blocked_by` refuses any restart while `close_pending` or `self.relay.draining` holds. So if the planner or a Claude implementer crashes once the PR is finished, the close waits forever and nothing restarts the agent. Before this change that session closed, and closing a dead pane loses nothing.

The same change also silences the stall watch (`StallWatch.busy` uses `idle_blockers`) for a crashed Claude frame whenever ExitWatch is barred without an alert: `restartExited: false`, the implementer.json tool-mismatch guard, or a recorded limit with no failover running. Before, that frame could reach a stall pointer and then escalate to blocked. Now the only trace is one `not restarting` log line.

Fix: Have Closer.agent_blockers skip the hard blocker when `limits.ps_prompt_last(text)` is true and the tail has settled. Or have ExitWatch alert when a guard bars the restart for longer than the grace period.

_confidence: 75 | sources: adversarial | lenses: adversarial | verdict: confirmed_

## Minor

### The exit mail's 'Next step' tells the planner to restart the agent, but start-github-issue.md tells it not to

`lib/relay.py:1013-1088`

`alert()` sends the planner `exit` mail with a `Next step: <step>` line (relay.py:1077). Elsewhere in the relay, 'Next step' is how it instructs the planner.

- The no-pin step says: 'Restart the agent in that pane by hand, then run `wb.py status active`.'
- The give-up step says: '... restart the agent with the pane's pinned command and run `wb.py status active`.'

The planner's contract, claude/commands/start-github-issue.md:271, says 'Do not restart the agent yourself ... leave it to the human.'

A planner that follows the mail could run the pin from its own shell and start a second implementer on the same checkout. It could also try to type into the implementer's pane, which the relay forbids. In the no-pin case, `wb.py status active` has nothing to undo, because nothing marked the loop blocked.

Fix: Address the step to the human, e.g. "Leave it to the human: they restart the agent with the pane's pinned command; then run `wb.py status active`."

_confidence: 60 | sources: docs+tests | lenses: docs, comments | verdict: refined_

### Module docstring's list of what the relay polls now leaves out the exit watch's tree read

`lib/relay.py:81-84`

The docstring still says the relay polls 'the two agent panes (for limits and stalls), and for stalls also the terminal's session tree and `git rev-parse HEAD`'. `ExitWatch.tick` now calls `agw.tree()` on every limit tick, even when the stall watch is off (`stallMinutes: 0`), and reads the panes for exits.

Fix: Change it to '... the two agent panes (for limits, stalls and exited agents), and the terminal's session tree (for exited agents and stalls) and, for stalls, `git rev-parse HEAD`'.

_confidence: 80 | sources: docs+tests | lenses: comments, docs | verdict: confirmed_

### README says a pane with no pin 'alerts the same way' as a give-up, but the no-pin alert is much smaller

`README.md:977`

The bullet just above describes the 4th-exit alert: blocked sound status, a notification, waiting.json, and in queue mode `loop-state blocked` with cause `environment`. The no-pin path (relay.py:1010-1014) calls `alert(...)` without `blocked=True` and never writes waiting.json or loop-state. It only notifies the planner pane and, for the implementer, mails `exit`. A reader of the README will expect a blocked status and a blocked queue slot that never appear.

Fix: Write: 'A pane with no pinned command is not typed into: the relay notifies the planner's pane (and mails the planner, kind `exit`, for the implementer), once.'

_confidence: 85 | sources: docs+tests | lenses: docs | verdict: confirmed_

## Sources

| agent | executor | model | effort | tokens | raised | status |
| --- | --- | --- | --- | --- | --- | --- |
| bugs+impl | claude | claude-opus-5-5 (requested opus) | high | 863712 | 3 | ok |
| arch+quality | claude | claude-opus-5-5 (requested opus) | high | 441547 | 3 | ok |
| docs+tests | claude | claude-opus-5-5 (requested opus) | high | 461664 | 5 | ok |
| adversarial | claude | claude-opus-5-5 (requested opus) | high | 1725153 | 3 | ok |
