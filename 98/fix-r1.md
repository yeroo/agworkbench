# FIX r1 - #98 (revmux round 1: 3 Major, 3 Minor; all verified against 85123e1)

Full report: `.workbench/review/revmux-r1.md`. Answer each finding: fixed / disputed (evidence) / deferred (reason).

## M1 - dry run still alerts and gives up for real (lib/relay.py ExitWatch.restart)
`restart()` checks `self.relay.dry_run` only after the no-pin branch (`alert` -> notify + `exit` mail)
and the budget branch (`give_up` -> blocked status, waiting.json, queue loop-state). Verified in the
diff: neither `alert` nor `give_up` checks dry run. A dry-run relay reading a live relay.json with 3
restarts would block the real loop. Fix: dry run checked right after `blocked_by`; log
`[dry-run] would restart / alert (no pin) / give up ...` once and return. Tests: dry run with no pin,
and dry run with the budget spent -> no notify, no mail, no waiting.json, no loop-state.

## M2 - give_up is not final for the episode (lib/relay.py ExitWatch.restart)
The `budget` note only stops a second `give_up`; on later ticks `recent()` is recomputed and, once the
oldest restart leaves the hour, the pin is typed - while waiting.json, the blocked status, queue
`blocked` and the "does not restart it again" alert stay. `test_budget_three_per_hour` (around lines
429-431) pins this. Fix: once `budget` is in the episode's notes, return before `recent()`; only a
new episode (pane seen running again, then exited) checks the budget again. Update that test to
expect no restart in the same episode, and add one that a new episode after the hour restarts.

## M3 - idle_blockers' early return makes an exited pane a hard blocker (lib/closer.py:712)
Before: a crashed Claude frame (rules, empty composer, footer, then the PS row) parsed as an empty
composer -> idle -> the close could proceed. Now it is a hard blocker in `Closer.agent_blockers`
(never overridden), and ExitWatch refuses to restart while `close_pending`/draining -> the close waits
forever on a dead pane. It also silences the stall watch when ExitWatch is barred without an alert
(`restartExited: false`, tool mismatch, a recorded limit), where a stall pointer / escalation used to
come.

My recommended fix: an exited pane has no agent in it, so it is **idle**, not blocking:
`idle_blockers` returns `[]` when `limits.ps_prompt_last(text)` (no busy check, no composer parse).
The close then closes a dead pane (nothing lost), and the stall watch runs its normal pointer and
escalation when the exit watch cannot restart. Drop AC11's wording from idle_blockers; instead, the
stall pointer body adds a line `<box> is at a shell prompt (its agent exited)` when a pane is, so the
planner/human sees why. Check that nothing that types into a pane uses "idle_blockers is empty" as
permission without its own agent check: ExitWatch.offer_pointer (only reached when not exited - keep
it that way, and add an explicit `ps_prompt_last` refusal there), closer.clear_stale_pointers and the
close's own typing. If you see a better fix, argue it. Tests: a crashed Claude frame + PS row with
close_pending -> the close is not refused on that pane; with restartExited false the stall pointer
comes and names the shell prompt; update `NoFalseStalls.test_an_exited_agent_is_named_as_such`.

## m1 - the exit mail's "Next step" tells the planner to restart the agent (lib/relay.py alert/give_up)
start-github-issue.md says "Do not restart the agent yourself ... leave it to the human". Reword
both steps for the human: "Leave it to the human: they restart the agent with the pane's pinned
command; then run `wb.py status active`" (give-up). For no-pin, nothing marks the loop blocked, so do
not ask for `status active`: "Tell the human; they restart the agent in that pane by hand."

## m2 - module docstring's polling list omits the exit watch (lib/relay.py:81-84)
Mention the exit watch's pane reads and its tree read (also when stallMinutes is 0).

## m3 - README says the no-pin case "alerts the same way" (README.md:977)
It only notifies the planner pane and, for the implementer, mails `exit`, once. Say so. Check the
same sentence in start-github-issue.md ("When it gives up, or the pane has no pin, ...") is exact.

After the fixes: whole suite through `wb.py suite`, commit, reply `FIXED <sha>` marking each of
M1, M2, M3, m1, m2, m3.
