# Review: workbench / r2-1

scope: `C:\Users\boris\source\workbench\agworkbench-issue-98\.revmux\tasks\workbench\r2-1\input\scope.md`

## Major

### Giving up is final only while the in-memory episode lasts: two 'not exited' reads or a relay restart re-arm the restart under a live waiting.json

`lib/relay.py:1015-1023`

The r1 fix makes giving up final with `if "budget" in episode["notes"]: return` (relay.py:1016-1019). That note lives only in `self.episodes[box]`, in memory. `restarts` is persisted in relay.json, but the give-up is not. `blocked_by` never checks the `waiting.json` that `give_up` wrote.

Two things drop the episode:
- `tick` pops it after LIMIT_READS (2) consecutive reads where `exited()` is False (relay.py:958-966). `exited()` is False whenever the last row is not a bare prompt or `foregroundShells` shows a child: a half-typed command, a pager, an editor, or any command still running at the read. None of these needs an agent.
- A relay restart (launcher run, stop file, agwinterm restart) also drops it, because `ExitWatch.__init__` starts with `episodes = {}`.

Failure path: the give-up alert tells the human to look at the pane, so they run `git status` or `git log` in the dead pane's shell. The episode ends. Later the pane sits at a bare prompt for the 2-minute grace and a new episode with empty notes reaches `restart()`. Two outcomes:
- While 3 restarts are still within the hour, `give_up` runs again: another blocked sound and notification, a second `exit` mail, waiting.json rewritten (even if the planner already reset status), and in queue mode loop-state blocked again.
- Once the oldest restart is past the hour, the relay types the pin plus Enter into the shell the human is using. waiting.json (`by: relay`) and the queue's blocked state stay in place.

This breaks the documented contract 'the pane is left alone until it runs an agent again' (relay.py:77, README:975-976, comment at relay.py:1016-1017). The 'no pin' note repeats the same way. No test covers either case:
- `test_budget_survives_a_relay_restart` restarts the relay before the give-up, not after it.
- `test_episode_ends_after_two_reads` and `test_a_new_episode_after_the_hour_restarts_again` bring the pane back only with a live agent frame.

Fix: Persist the give-up per box in relay.json (e.g. `state['exitGaveUp'][box] = wall()`) and check it in `restart()` before `recent()`. Clear it only when an agent is positively seen running (a composer parses through `peerchat.composer_content`, or `is_busy`) on LIMIT_READS reads, not on a plain 'not a bare prompt' read. Do the same for the 'no pin' note. At minimum, skip any restart while waiting.json says `by: relay` with this reason. Add tests for a human command at the prompt after a give-up and for a new Relay built after a give-up.

_confidence: 99 | sources: adversarial, docs+tests, bugs+impl | lenses: adversarial, comments, docs, tests, bugs, impl | verdict: confirmed_

## Minor

### Restart types the pin with raw agw.type_into from pane text read at the start of the tick, with no fresh re-read

`lib/relay.py:1037-1039`

`ExitWatch.restart` decides from the `texts` that `read_panes()` captured at relay.py:2402. Between that read and `agw.type_into(peer.pane, pin + "\n")` at relay.py:1038, the tick runs `check_limits`, which can type a probe into the other pane through peerchat and take seconds, plus `agw.tree()` and `blocked_by`. The pane is never re-read. Every other typing path re-reads first, through peerchat. This call also contradicts the README-style rule at relay.py:89 that typing into a pane goes through peerchat, and that a pane that is not an agent is a refusal.

Trigger: a human starts typing at the bare prompt inside that window, right after a 2-minute stable grace. The pin and Enter are then appended to their partial input and run as one line. The usual result is a garbled command that errors out.

The window is short and the pane has already sat unchanged for 2 minutes, so this is rare. The point about control bytes in the pin is hypothetical: pins are written by the launcher.

Fix: Just before `type_into`, re-read with `agw.pane_text(peer.pane)`. Type only if `limits.ps_prompt_last(fresh)` holds and `limits.tail_hash(fresh) == episode['tail']`. Amend the rule at relay.py:89 to name this exception.

_confidence: 50 | sources: arch+quality, bugs+impl | lenses: architecture, bugs | verdict: refined_

### The ExitWatch docstring and README intro still say the stall watch reads an exited pane as not idle

`lib/relay.py:889-890`

After 5588554, `closer.idle_blockers` returns [] for a bare PS last row (closer.py:712-715). `StallWatch.busy` therefore counts an exited pane as idle, and the stall watch now points at it and names it (relay.py:819-821, `test_an_exited_agent_is_idle_and_the_pointer_names_it`).

Two docs still describe the old behaviour:
- The ExitWatch docstring (relay.py:889-890): 'peerchat holds its mail, the stall watch reads it as not idle.'
- The README intro (README.md:955-956), in the present tense: 'the stall watch only sees a pane that is not idle'.

Both contradict the README bullet a few lines below ('An exited pane counts as idle ... the stall watch's pointer comes as usual').

Fix: relay.py:890: '... peerchat holds its mail, and before #98 the stall watch read it as not idle.' (or drop the clause). README.md:955-956: say the stall watch *used to* read the pane as busy.

_confidence: 90 | sources: docs+tests | lenses: comments, docs | verdict: confirmed_

### The new ExitedAgent class replaced the NoFalseStalls header, so all the no-false-stall tests now run under ExitedAgent

`tests/test_stall.py:326`

The hunk in 5588554 rewrote `class NoFalseStalls(StallFixture):` as `class ExitedAgent(StallFixture):` and put the new test above the old body. It did not add `ExitedAgent` as a separate class, so `NoFalseStalls` no longer exists.

Everything from `assert_quiet` through `test_dry_run_only_logs` (about lines 339-453) now belongs to `ExitedAgent`, including:
- `test_a_running_helper`
- `test_a_pr_open_for_review`
- `test_loop_reports_and_records`
- `test_panes_not_provably_idle`
- `test_off_when_stall_minutes_is_zero`

The tests still run, but anything that selects `tests.test_stall.NoFalseStalls` (or `-k NoFalseStalls`) now finds nothing. Failure output also reports exemption regressions as `ExitedAgent` failures.

Fix: Put `class NoFalseStalls(StallFixture):` back above `def assert_quiet` so that `ExitedAgent` holds only its own test.

_confidence: 99 | sources: docs+tests, bugs+impl | lenses: tests, impl | verdict: confirmed_

## Immaterial

### restart_exited_setting is close_helpers_setting pasted with one key changed

`lib/relay.py:386-402`

`restart_exited_setting` (relay.py:386-402) is a line-for-line copy of `close_helpers_setting` (relay.py:365-383). They share the same path lookup, the same FileNotFoundError/OSError/ValueError branches, the same not-a-dict check and the same casefold loop. Only the key literal differs.

Any later fix to the fail-closed rules (two disagreeing spellings, a BOM) now has to be made twice. A missed copy would make the two switches read the same config differently.

Fix: Add one `_bool_setting(key: str) -> tuple[bool, str]` that holds the shared body, and make both functions one-line calls to it.

_confidence: 80 | sources: arch+quality | lenses: quality | verdict: immaterial_

## Sources

| agent | executor | model | effort | tokens | raised | status |
| --- | --- | --- | --- | --- | --- | --- |
| bugs+impl | claude | claude-opus-5-5 (requested opus) | high | 2485142 | 4 | ok |
| arch+quality | claude | claude-opus-5-5 (requested opus) | high | 994877 | 3 | ok |
| docs+tests | claude | claude-opus-5-5 (requested opus) | high | 1584347 | 3 | ok |
| adversarial | claude | claude-opus-5-5 (requested opus) | high | 1560460 | 1 | ok |
