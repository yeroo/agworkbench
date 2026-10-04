# Plan v2 - #98 Relay doesn't notice when an agent process exits (pane back at a shell prompt)

Issue: `.workbench/issue.md`. Branch `issue-98-relay-doesn-t-notice-when-the-im`, base `origin/main`.

## Changes from v1 (answering the critique)

> "a faithful `Test-ShellReady` port misses a crashed agent ... ExitWatch must not use it"

Conceded, verified: Test-ShellReady refuses any frame whose last 15 rows hold `context: N%`, Kimi's
box, Claude's rules or `Chat from Workbench`, and a crash leaves exactly those above the prompt.
Detection is now `foregroundShells[i]` + a bare `PS X:\...> ` last row (`limits.SHELL_PS_RE`) +
stability for a grace period. I disagree with keeping `shell_ready` as an unused port: dead code
with its own tests is a second rule nobody calls, and it would drift from the PowerShell one.
Dropped (v1's AC1 removed).

> "restarting a limited tool after a failover that did not finish"

Conceded, verified (Set-ImplementerLimit writes `limits[<tool>]` to state/implementer.json; the
relay's episode is popped after two misses once Clear-Host has run). Guards (a) and (b) added.

> "fighting a human who exited on purpose" - grace period and `restartExited: false`

Conceded both. Grace: the exited state unchanged (same tail hash) for EXIT_GRACE_MINUTES = 2 of wall
time. The switch is read like `close_helpers_setting` (any key case; fails closed).

Smaller points all taken: crashed-frame tests; a tree without `foregroundShells` logged once; the pin
taken from the same snapshot as the detection, right before typing; episode end defined (AC10).

## Goal

When an agent (planner or implementer, any tool: claude, codex, kimi) exits or crashes and its pane
falls back to the root shell's prompt, the relay notices on its limit tick instead of reading the
pane as "composer is not provably empty" forever. It types the pane's pinned restore command (what
agwinterm runs on restart; every pin already resumes the conversation), logs `agent exited;
restarted with resume`, and once the agent's composer is up types one resume pointer. At most 3
restarts per pane per rolling hour; the next exit alerts and marks the loop blocked.

## Facts from the code (verified)

- The tree gives each session `restoreCommands` {pane id: command} and `foregroundShells` (pane
  order; a recognized live root shell with *no child process*, else null). Our agent panes are an
  interactive pwsh into which the launcher typed `pwsh ... -File pane-*.ps1 ...`; while the agent
  lives the entry is null, after it exits it is `pwsh`.
- Pins: planner `pane-claude.ps1` (resumes its transcript with a resume prompt), Claude implementer
  `pane-implementer-claude.ps1` (same), Codex `pane-codex.ps1 -Resume`, Kimi
  `pane-implementer-kimi.ps1 -Resume` (mails itself a `resumed after restart` note). The pin is the
  resume command for every tool; no per-tool table.
- `launch.lock` is held (FileShare.None) for the whole launcher run, incl. `-Failover`;
  `conductor.file_locked` tests it.
- `limits.classify` reports an agent that exited at its usage limit as an episode in
  `relay.state['limits']`; failover/wait owns that case.

## Acceptance criteria

1. **Exited**, for a pane, means in one `agw.tree()` snapshot and one pane read: its
   `foregroundShells[i]` is a non-empty string AND the last non-empty row matches `limits.SHELL_PS_RE`
   (bare `PS X:\...> `, nothing typed after it). Rows above it are not inspected.
2. A pane exited on consecutive reads with the same `limits.tail_hash` for at least
   EXIT_GRACE_MINUTES (2, wall clock) is restarted: exactly one `session.type` of
   `<restoreCommands[pane]>\n`, the pin from that tick's snapshot; log
   `agent exited; restarted with resume: <box> (<tool>) attempt k/3` (k = restarts in the last hour).
   Any tail change (the human typing, output) restarts the grace clock.
3. Crashed frames are restarted: `tests/fixtures/kimi/idle-after-turn.txt` + a prompt row; a Claude
   idle frame (rules + `bypass permissions`) + a prompt row; a frame with a `Chat from Workbench: ...`
   row above the prompt - each with foregroundShells `pwsh`.
4. After the restart, when `closer.idle_blockers(peer, text)` is empty, one pointer
   `Chat from Workbench: <RESTART_TEXT>` is typed through peerchat (retry=False; Refused is retried
   next tick). Never typed while busy or at a shell; dropped with one log line after
   RESTART_POINTER_MINUTES (10). For Claude panes the pin already starts a resume turn, so the pointer
   is a second short turn after it; documented.
5. Not touched (no type, no count): foregroundShells null or missing for the pane; last row not a
   bare PS prompt (a dialog, `[y/N]`, a draft, a running agent, Kimi shell mode, a prompt with typed
   text) - tested with null and with an inconsistent `"pwsh"` plus an agent frame; exited for less
   than the grace period.
6. No restart (each reason logged once per episode) while: `restartExited` is off
   (`~/.agworkbench.json`, any key case; anything but true/null is off; unreadable config is off);
   `launch.lock` held; `state/loop-done.json` exists; `close_pending`, or the relay is draining a
   finished PR; `relay.state['limits']` has an episode for the box; for box codex, (a)
   `state/implementer.json` has `limits[peer.tool]`, or (b) its `tool` != `peer.tool`; `--dry-run`
   (logs `[dry-run] would restart ...`).
7. No pin for the pane: no type; one log line and one alert (notify on the planner pane; when the
   dead pane is the implementer, also mail the planner from `relay`, kind `exit`).
8. A tree whose session has no `foregroundShells`: `exit watch: the terminal reports no
   foregroundShells; exited agents are not detected` logged once.
9. Budget: restart wall times per box in relay.json (`restarts: {box: [t, ...]}`, pruned past 1 h),
   surviving a relay restart. An exit while 3 restarts lie within the last hour is not restarted:
   `ALERT agent exited ...` logged; planner pane blocked+sound and notified; `state/waiting.json`
   (`by: relay`, the reason); queue mode `loop-state blocked` with cause `environment`; the planner
   mailed (kind `exit`) when the dead pane is the implementer. Once per episode.
10. An episode (the exited state, its grace clock, its once-only logs and alerts) ends when the pane
    reads not-exited on 2 consecutive reads.
11. The shared `closer.idle_blockers` reason for a pane whose last row is a bare PS prompt reads
    `<box> is at a shell prompt (its agent exited)` (wording only; the close still refuses).
12. `python -m unittest discover -s tests` passes.

## Approach

- `lib/limits.py`: `ps_prompt_last(text) -> bool`, used by ExitWatch and idle_blockers.
- `lib/relay.py`: `restart_exited_setting()` beside `close_helpers_setting`; a new `ExitWatch`
  beside `StallWatch`, `self.exits = ExitWatch(self)`, ticked in `run()` right after
  `check_limits(texts)` and before `self.stall.tick(texts)`, in its own try/except-and-log so a bug
  never stops the doorbell. Per tick: one `agw.tree()`; per peer: find the session for `peer.pane`,
  its index, `foregroundShells[i]`, `restoreCommands[pane]`; exited per AC1; grace per AC2; guards
  (AC6), budget (AC9), pin (AC7); then `agw.type_into(pane, cmd + "\n")` (as the launcher types
  `Clear-Host`), record the time, set a pending pointer. Pending pointer per AC4, sent like
  `Relay.probe` does. RESTART_TEXT: "your agent process exited and the relay restarted it; continue
  where you left off (git status, .workbench, unread mail)". No `[id X]`, so the #96 rescue never
  touches it.
- Mail delivery needs no change: peerchat refuses a pane that is not an agent, so mail to a dead
  pane is held and rung after the restart.
- Docs: relay.py module docstring job 7 "Exited agents (#98)"; README's relay section and
  `claude/commands/start-github-issue.md` (what the relay does, the 3/hour cap, `restartExited`,
  that a blocked `agent exited` is the human's: restart the pane with its pinned command, then
  `wb.py status active`).

Rejected: restarting from the stall watch only - it is exempted by unread mail, an open PR, CI, the
very states a dead implementer leaves hanging. Rejected: `foregroundShells` alone (not proof per
agwinterm) or the prompt row alone (an agent can print `PS C:\>`). Rejected: the full Test-ShellReady
rule (refuses crashed frames).

## Files

- `lib/limits.py` - `ps_prompt_last`.
- `lib/relay.py` - `ExitWatch`, `restart_exited_setting`, constants, wiring, docstring.
- `lib/closer.py` - `idle_blockers` wording.
- `README.md`, `claude/commands/start-github-issue.md` - docs.
- `tests/test_exit_restart.py` (new), `tests/test_limits.py`, `tests/test_stall.py`.

## Tests

New `tests/test_exit_restart.py` (patching `agw.tree`, `agw.pane_text`, `agw.type_into`,
`peerchat.send` and the clocks, in the style of tests/test_stall.py; no terminal):
- `test_pwsh_prompt_restarted_after_grace_and_gets_pointer` (kimi `-Resume` pin): no type before
  2 min, one type after; the pane then shows `kimi/idle-fresh.txt` -> one pointer; later ticks type
  nothing.
- `test_planner_pane_restarted`, `test_codex_restarted`.
- `test_crashed_kimi_frame_restarted`, `test_crashed_claude_frame_restarted`,
  `test_pointer_row_above_prompt_restarted`.
- `test_tail_change_resets_grace`, `test_foreground_null_not_restarted`,
  `test_agent_frames_not_touched` (`[y/N]`, `trust-dialog.txt`, `draft-wrapped.txt`, `shell-mode.txt`,
  a running frame, `PS C:\x> git status` - each with null and with `"pwsh"`).
- `test_guard_*`: restartExited false / unreadable config; launch.lock held; loop-done;
  close_pending; draining; limit episode; implementer.json limits[tool] with no episode;
  implementer.json tool mismatch; dry run - no type, each logged once over several ticks.
- `test_no_pin_alerts_once`, `test_no_foreground_shells_logged_once`.
- `test_budget_three_per_hour` (4th exit: no type, waiting.json, mail kind exit, blocked status, no
  re-alert; an exit after the hour restarts again; the budget survives a new Relay on the same
  relay.json).
- `test_queue_mode_environmental_block`, `test_pointer_deadline`, `test_episode_ends_after_two_reads`.
- `tests/test_limits.py`: a `ps_prompt_last` table. `tests/test_stall.py`: the new wording.

Run: `python -m unittest discover -s tests`; focused:
`python -m unittest tests.test_exit_restart tests.test_limits tests.test_stall -v`.

## Out of scope

- A pane whose own process ended (a `--command` direct pane: no shell to type into).
- Restarting an agent that exited at its usage limit: the limit path owns it.
- Changing the pane scripts or their resume prompts.

## Open questions

None left from v1: the pointer stays (documented); queue cause `environment` agreed.
