# FIX r2 - #98 (revmux round 2: 1 Major, 3 Minor, 1 Immaterial; all verified against 5588554)

Full report: `.workbench/review/revmux-r2.md`. Mark each fixed / disputed (evidence) / deferred (reason).

## M1 - giving up is final only while the in-memory episode lasts (lib/relay.py ExitWatch)
Verified: the `budget` (and `no pin`) note lives in `self.episodes[box]`; `tick` pops the episode after
2 not-exited reads (a half-typed command, a pager, any running command - `foregroundShells` has a
child), and a relay restart starts with `episodes = {}`. `blocked_by` never looks at the waiting.json
`give_up` wrote. So after the give-up a human using that shell (as the alert tells them to) re-arms
it: either a second give_up (sound, mail, waiting.json rewritten, queue blocked again) or, past the
hour, the pin typed into the human's shell under a live waiting.json.
Fix as the report proposes: persist the give-up per box in relay.json (`exitGaveUp: {box: wall}`), and
the no-pin alert the same way (`exitNoPin: {box: wall}`); check them in `restart()` before `recent()`.
Clear them only when an agent is **positively** seen running in the pane on LIMIT_READS consecutive
reads (a composer parses via `peerchat.composer_content(peer.tool, text)` is not None, or `is_busy`),
never on a plain "not a bare prompt" read. Episode end (for the grace clock) can stay as it is; the
give-up latch is separate. Tests: after a give-up, a human command at the prompt (not-exited reads,
then a bare prompt for > grace) -> no type, no second alert; a new Relay on the same relay.json
after a give-up -> no type, no alert; a live agent frame twice -> latch cleared, a later exit is
judged afresh (budget permitting). Same pair for the no-pin latch.

## m1 - restart types the pin from a pane read taken at the start of the tick (relay.py ~1038)
Re-read just before `type_into`: type only if `limits.ps_prompt_last(fresh)` and
`limits.tail_hash(fresh) == episode['tail']`, else leave the grace clock to restart. Name this
direct-type exception in the module docstring's "Typing into a pane goes through peerchat" paragraph.
Test: the pane changes between the tick's read and the re-read -> no type.

## m2 - docs still say the stall watch reads an exited pane as not idle
relay.py ExitWatch docstring (~line 890) and README.md ~955-956: past tense / drop the clause.

## m3 - tests/test_stall.py:326 renamed NoFalseStalls to ExitedAgent
Restore `class NoFalseStalls(StallFixture):` above `def assert_quiet`, leaving ExitedAgent with only
its own test.

## i1 - restart_exited_setting duplicates close_helpers_setting (relay.py ~365-402)
Cheap: one `_bool_setting(key)` with the shared body, both functions one-line calls. Keep the
existing tests passing unchanged.

After the fixes: whole suite through `wb.py suite`, commit, reply `FIXED <sha>` with each item marked.
