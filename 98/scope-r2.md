# Review scope r2 - yeroo/agworkbench#98

Change: the relay restarts an agent (planner or implementer, any tool) whose process exited and left
its pane at the root pwsh prompt, using the pane's pinned restore command; one resume pointer after;
at most 3 restarts per pane per hour, then alert + loop blocked. Plan: `.workbench/plan.md` (v2,
agreed). Issue: `.workbench/issue.md`.

Diff range: `origin/main..HEAD` (4a1e8f3, 85123e1, and the r1 fixes 3de4114, 5588554).

## Acceptance criteria
See plan.md "Acceptance criteria" 1-12. Key ones: detection = `foregroundShells[i]` non-empty AND
last non-empty row a bare `PS X:\...> ` (rows above ignored: a crash leaves the TUI frame), stable
(same tail hash) for 2 min wall time; guards (restartExited off, launch.lock held, loop done,
close_pending/draining, limit episode, implementer.json limits[tool] or tool mismatch, dry run);
budget kept in relay.json; pointer only on an idle, empty composer, dropped after 10 min.

## Look hardest at
- `lib/relay.py` `ExitWatch`: can it ever type into a pane where an agent is alive, a human is typing,
  or a failover/launcher is mid-way? Can it type the old (limited) tool's pin?
- Budget and episode bookkeeping across ticks and relay restarts; once-only logging; give_up writes.
- `closer.idle_blockers` early return: does it change any existing close or stall decision beyond
  the wording?
- `lib/hub.py` KINDS gained `exit`; `lib/Workbench.ps1` validates `restartExited`.
- Tests in `tests/test_exit_restart.py`: do they actually exercise the guards (fail without them)?

## Out of scope
Direct-mode panes whose process ended; restarting an agent that exited at its usage limit; pane
scripts' resume prompts.

Do NOT run interactive or GUI tests, and do not touch a live agwinterm. Unit tests:
`python -m unittest tests.test_exit_restart tests.test_limits tests.test_stall -v`.

## Round 2 focus
Round 1 (`.workbench/review/revmux-r1.md`) found: dry run alerting for real; give-up not final for
the episode; an exited pane made a hard close blocker. Fixed in 3de4114/5588554:
- `closer.idle_blockers` now returns [] for a bare PS last row (an exited pane is idle).
- `peerchat.at_shell` (PS prompt last row, with or without text) makes every composer parser return
  None, so no send/resubmit/clear/rescue types into a dead pane. Check this cannot refuse a live
  agent (could any live Claude/Codex/Kimi frame end in a `PS X:\...>` row?) and that nothing else
  that types keys bypasses the parsers.
- ExitWatch: dry run passive; budget final per episode.
Verify the fixes and look for regressions they introduce.
