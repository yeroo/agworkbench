# Review scope r1 - yeroo/agworkbench#98

Change: the relay restarts an agent (planner or implementer, any tool) whose process exited and left
its pane at the root pwsh prompt, using the pane's pinned restore command; one resume pointer after;
at most 3 restarts per pane per hour, then alert + loop blocked. Plan: `.workbench/plan.md` (v2,
agreed). Issue: `.workbench/issue.md`.

Diff range: `origin/main..HEAD` (commits 4a1e8f3, 85123e1).

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
