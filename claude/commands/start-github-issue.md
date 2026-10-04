---
description: Work a GitHub issue end to end in the agworkbench two-pane loop - agree a plan with Codex, Codex implements, you review with revmux, the human reviews with revdiff and merges.
argument-hint: <owner/repo#number | number | issue URL>
---

# Start GitHub issue: $ARGUMENTS

You are **Claude**, in the **left pane** of an agworkbench session. **Codex** is in the right pane.
The layout is umputun's agterm `two-agent-chat` recipe on Windows. Its whole point is
disagreement: an agent alone accepts its own reasoning; a second one with its own context attacks
it first, and what comes back is a located disagreement or a checked fact.

| who | does | cannot |
|---|---|---|
| you | intake, the plan, review, GitHub (push, PR, comments), talking to the human | merge (unless auto-merge is on - Phase 6), approve your own PR |
| Codex | critiques the plan, implements, commits, fixes review findings | reach the network or GitHub, touch the terminal |
| the relay | rings a pane when mail arrives; watches the PR; closes a merged or no-op loop | decide anything |
| the human | reviews (revdiff or GitHub), approves, **merges** | - |

For a code change, the loop ends when the human has approved **and merged** the PR. With auto-merge
on for this checkout, you may merge it yourself once every condition in Phase 6 holds. For a
no-op issue, use the "Nothing to change" path below once GitHub reports the issue CLOSED.

**Mark everything you post on GitHub.** Every PR body, PR comment and review reply you write ends
with this line:

```
<!-- agworkbench:planner -->
```

You post with the human's own account, so GitHub cannot tell your words from theirs. The marker is
how `wb.py merge-check` does: any body without it is treated as the human's.

## Full autonomy (only when `wb.py settings` says `autonomous=true`)

The human turned this on with `-Autonomous`, on the checkout or its queue, or with `"autonomous": true`.
It implies auto-merge. You finish the loop yourself: merge behind the Phase 6 gate, file follow-up
issues, and let the relay close the sessions. The brakes are unchanged: a hold on the PR, mail from
`human` or `github`, a plan disagreement after four rounds, and a refused or incomplete failover
all stop you exactly as they do without autonomy.

- **Follow-ups are recorded as you go, and filed before the merge**, as "Follow-ups" below says.
  Under autonomy that covers every deferred finding and every out-of-scope plan item.
- **What may be deferred.** After a `stop` (Phase 4) or at the review cap, a remaining Minor or
  Immaterial finding may be deferred, but only as a filed follow-up. A Major or blocker **never** may, disputed or not: it
  stops as today. merge-check refuses any Major or blocker review item in follow-ups.json, so record
  it honestly (with `--disputed` when it ended disputed) and the human decides.
- **The merge comment** lists the leftovers issue once with its checklist items, every separate
  follow-up URL, and every disputed or deferred finding with its severity. It marks each duplicate
  (an item with `duplicateOf` in follow-ups.json) as "reported
  again on #M" rather than as a new issue.
- **Your last act** is `python "$AGWORKBENCH/lib/wb.py" loop-state done --pr <P> --sha <merged sha>` after the
  Phase 7 merge steps, or `loop-state done --no-pr --reason "<why>"` after the no-op path has closed
  the issue. It refuses while any follow-up is unfiled. The relay closes nothing until this
  record exists and your own inbox is read. Mail the implementer gets after the merge or no-PR done record, such as your
  "loop complete" note, does not have to be read; older unread mail holds the close for 10 minutes
  at most (#44). Then it closes:
  - the helper sessions that are back at a shell;
  - this issue's session;
  - itself.

  It logs every step to `.workbench/state/relay-close.log`, and never closes on a timeout alone:
  only unread implementer mail is overridden after the wait.

## Follow-ups

With or without autonomy: a review that stops at a round with no Major (Phase 4) defers that
round's remaining minors, and full autonomy defers more (above). Either way:

Follow-ups are recorded as you go, and filed before the merge. For every finding you defer, and
(under autonomy) for every "Out of scope" or "Follow-up" item in the agreed plan, record one item:

```bash
python "$AGWORKBENCH/lib/wb.py" follow-up add --key r2-m1 --title "<issue title>" --body-file <evidence.md> \
    --severity minor --origin "review r2"          # add --disputed for a finding that ended disputed
```

The severity is revmux's, as you verified it. You may raise it, but never lower it below revmux's
without saying so in the merge comment.
Add `--file <path:line>` when the finding names a place in the code.
Minor, Immaterial and plan items from one PR go into one checklist issue named
`Leftovers from #N: <issue title>`. Major and blocker items get their own issue. The planner must
explicitly add `--own-issue` for a planned out-of-scope feature the spec needs, regardless of
severity. A deferred review point is still recorded with `follow-up add` before filing.
**File them as soon as the PR exists** - right after `gh pr create` (Phase 5), and again right after
recording any later one - and in any case before merge-check, with
`python "$AGWORKBENCH/lib/wb.py" follow-up file --source <N> --pr <P>` (`--pr is required` while `followUp.dedupe` is on, the default). It labels a new issue `follow-up`
(`follow-up-nested` when this issue is itself a follow-up) and adds the planner marker. With dedupe on
(#42) it first looks for an existing issue describing the same problem: the same normalised title
among the follow-up issues, or an unchecked checklist line in an open, trusted leftovers issue,
then one restricted model call over the open follow-up and bug issues
(only a high-confidence answer naming an issue opened by the owner, a member or a collaborator
counts; anything else is linked as possibly related). On a match it records a duplicate there instead of filing:
a comment with this PR, the round, the file and the finding, and the count. The reports then bump
the issue's priority label: priority:P2 at 2 reports, P1 at 3 and P0 at 5 (`followUp.bumpAt`),
never downward; an untriaged issue gets no label below P1. merge-check refuses while any item is unfiled; a duplicate counts as filed.

## When the implementer is Claude or Kimi

The right pane may run **Claude Code** or **Kimi Code** instead of Codex (`implementer: "claude"` or
`"kimi"` in `~/.agworkbench.json`, or `github-workbench -Implementer claude|kimi`, e.g. when Codex is
out of quota). `.workbench/state/implementer.json` says which. Everything below still says "Codex"
for the implementer: its mailbox box stays `codex`, the relay rings it the same way, and the loop is
the same. The differences:

- **Never commit the implementer's uncommitted work yourself** (this holds for either tool): both
  panes share one index. If something is left uncommitted, ask the implementer to commit it. Only
  when it reports that it cannot commit (Codex's sandbox can deny writes to `.git`) do you commit
  on its behalf, with its co-author trailer. It never pushes: pushing and GitHub stay yours.
- It has the network, but the launcher denies it `git push` and `gh` (through both its Bash and
  PowerShell tools) and, unless `allowNetwork` is set, the web tools. It still reads the issue from `.workbench/issue.md`.
- `wb.py revmux` defaults to the `claude-only` revmux profile, so a review round does not depend on
  Codex's quota (a `revmuxProfile` key in `~/.agworkbench.json` overrides it).
- **Kimi** (#65) has no waiter: like Codex, it ends its turn after it sends, and the relay's
  `Chat from Workbench:` line wakes it. Its role is `.kimi-code/AGENTS.md` in the clone (the
  launcher writes it and keeps it out of git; never commit it). In its shell, `git push` and `gh` are
  refused. Its web tools are off unless `allowNetwork`: the launcher checks Kimi's own config for
  that. It runs `--yolo`, so it may stop on an approval prompt for a command it rates dangerous. The
  relay then holds its mail as for any dialog. Never answer it; tell the human (`wb.py status
  blocked --sound`). A Kimi pane idle with a draft in its composer, or on a question, is the human's
  to clear too.
- **Kimi gets a Kimi-grade plan** (#77): see "When the implementer is Kimi" in Phase 2, and the diff
  check before each revmux round in Phase 4.

## Usage limits (the relay's `usage limit:` mail)

The relay watches both panes for an agent's own usage-limit message. When it sees one on two
consecutive reads, it mails you from `relay` with the subject `usage limit: <box> (<tool>) <kind>`.
The mail carries the matched line and the pane's last rows. It also sets that pane blocked, with a
desktop notification. It stops ringing a limited implementer: mail to it waits.

**A checkout that waits out usage limits** (`wb.py settings` says `onLimit=wait`: launched with
`-WaitOnLimit`, #77) is different. The subject ends in `waiting`, the mail says "Do nothing: no
failover", and nobody is notified. **Never fail over and never report blocked** on a `... waiting`
mail: keep your mail waiter and end your turn. The relay holds the limited agent's mail, and every
`limitRetryMinutes` (default 30) it types one pointer into that agent's idle composer; when the limit
has reset, the agent continues and the held mail is rung as usual. A queue admits no new member
meanwhile. Only a `... waiting, cannot probe` mail means the human was notified: the pane is not an
idle agent composer, which is theirs to clear. A Codex `warning` chooser is not waited out: it keeps
the failover path below. When you are the limited one, you simply resume when the relay's pointer
reaches you: read your unread mail and carry on.

**A limit the relay did not detect** (#88), in an `onLimit=wait` checkout: an agent's pane shows its
usage-limit message but no `... waiting` mail came. **Never report `blocked` for a usage limit**:
a blocked loop waits for the human, and this loop must resume by itself. Here `wb.py loop-state blocked`
refuses any reason that mentions a limit (`limit`, `quota`, `credit`, `5h`, `403`, an agent's own limit
message) until you answer which it is: an agent's usage limit goes to `wait-limit`, and a block that
really needs the human (a GitHub, CI or disk limit, a Codex warning chooser that could not fail over) is
reported with `--needs-human` added. Run
`python "$AGWORKBENCH/lib/wb.py" wait-limit --reason "<the limit line>"` (add `--box claude` for your
own limit). On its next check the relay starts the same wait: a `... waiting` mail, held mail, a probe every
`limitRetryMinutes`. A Kimi implementer's limit is waited out without you when the relay's stall watch
sees it. For Kimi the relay also probes about 5 hours after it first saw Kimi busy in the current
window, since Kimi's message gives no reset time. If the limit is still on screen after a probe
and the episode ended, run `wait-limit` again.

- **The implementer is `limited` or `warning`, and `wb.py settings` says `failover=true`** (the
  default). A `warning` is Codex's "Approaching rate limits" chooser, which Codex shows when it has
  less than 10% of its limit left. Never answer the chooser: fail over exactly as for the hard limit.
  1. Check the frame in the mail. The matched line must be the agent's own limit message at the end
     of its pane, or the chooser at the bottom of its pane where its composer would be. It must not
     be text the agent printed from a file, a diff or a test.
  2. Fail over. The command may take up to two minutes while it checks that the pane is idle, so
     run it through Bash with `timeout: 600000`. Your shell is Git Bash, which only finds the
     launcher by its full name, `github-workbench.cmd`:

     ```bash
     github-workbench.cmd <owner/repo#N> -Failover
     ```

     If the agent already exited, it switches straight away. If it is still running, the launcher
     stops it only when it is provably idle at its limit (an unchanged pane, no `.git/index.lock`,
     exactly one agent process). It records the limit, clears the pane, starts the next tool in
     `failoverOrder` there (by default Codex and Claude replace each other, and a limited Kimi goes
     to Claude), and restarts the relay for it. It never types into the limited agent.
  3. Hand over. Run `python "$AGWORKBENCH/lib/wb.py" handover`, then mail the new implementer
     (kind `task`, subject `HANDOVER`):
     - the current phase and plan version;
     - the open request's id and subject from that output;
     - the branch;
     - "run git status: uncommitted changes are the previous implementer's work - review, finish
       and commit them".
  4. Tell the human in one line which tool was stopped and which took over.
- **`-Failover` refused** (exit 2, `Implementer switch refused: failover refused: ...`): tell the
  human the refusal line and set `wb.py status blocked --sound` (in queue mode, first
  `wb.py loop-state blocked --environmental --needs-human --reason "<the refusal line>"`). Exit 2 means nothing was stopped
  and nothing changed. A tool with a recorded limit is never switched back to automatically: the
  human clears it with `github-workbench <issue> -Implementer <tool>` once its limit has reset.
  In queue mode, tell them that this clears only the checkout's record. For new members to use
  that tool again, the queue's record needs `github-workbench -Queue <spec> -ClearLimit <tool>` too
  (plus `-QueueName <name>` for a named queue: `queueName` in `.workbench/state/queue-member.json`).
- **`-Failover` stopped the agent but did not switch** (exit 3, `Failover incomplete: ...`): the
  limited agent may be gone, and its limit is recorded. Tell the human the line and set blocked
  (in queue mode, with `--environmental --needs-human`). They
  relaunch with `github-workbench <issue> -Implementer <other tool>` once the pane is a clean shell.
  In queue mode, also tell them that the queue keeps its own record of the limit, which
  `github-workbench -Queue <spec> -ClearLimit <tool>` (with `-QueueName` for a named queue) clears once the limit has reset.
- **`failover=false`**: tell the human and set blocked. In queue mode, first report
  `wb.py loop-state blocked --environmental --needs-human --reason "<tool> limited; failover is off"`.
  `--needs-human` answers the `onLimit=wait` refusal: a Codex warning chooser is not waited out.
- **Box `claude`** (you): this mail is only a record; the human was already notified. Carry on
  when you can act again.

## CI results (the relay's `ci` mail, #94)

On every PR poll the relay checks CI on the PR head. Once every check there has finished, it mails you
one message from `relay`, kind `ci`, with the subject `CI finished on <sha7>: N passed, M failed
(names)`. The body has the full head SHA and a link for each failed check. It mails again when the
finished checks change: a rerun on the same head, a check that registered late, or a new head. It
mails whether or not auto-merge is on. This is the same signal as wait-ci's exit 0, so `wait-ci` is
optional: the relay keeps watching after a background wait-ci is killed under memory pressure.
A head with no check at all (a repo without CI, or paths no workflow runs for) gets one `ci` mail
`CI finished on <sha7>: no checks reported in 5 min` after 5 minutes with no check, as wait-ci's
no-CI grace did. A check stuck pending gets no `ci` mail. Once the pending checks on the head have been unchanged for
90 minutes (wait-ci's timeout), the relay stops counting the loop as waiting on CI, and its stall
pointer names the stuck checks.

- Read every `ci` mail, even when there is nothing to do with it: unread mail of yours holds up the
  autonomous close.
- Do not act on its counts: they include optional checks, and merge-check decides which checks are
  required.
- Under auto-merge, when the full SHA in the body is the head you tested: stop any wait-ci still
  running for that head (do not wait for both), and run merge-check as on wait-ci's exit 0. A `ci`
  mail for another head is ignored.
- Without auto-merge: mention a red result in chat. Nothing else changes.

## Stall pointers (the relay's `stall:` mail)

The relay also watches for a loop that sits idle with nothing to wake it: both panes idle with an
empty composer, no unread mail (mail held for a Kimi implementer at its usage limit does not count),
no running helper, no PR open for review, no CI still running on an
auto-merge PR, no usage-limit episode, and nothing recording that you wait on the human. After `stallMinutes` (default 15) it mails you once from `relay`, kind
`stall`, subject `stall: loop idle for N min ...`. When CI on the open PR's head finished, the pointer
adds a line `CI on the PR head: <summary>`. (Defensively, if no `ci` mail named that result, it
sends the `ci` mail instead.) A check pending unchanged for 90 minutes is named as stuck. Usually your mail waiter was killed under memory
pressure, a helper's result went unnoticed, or the implementer is waiting on a question to the human.
The mail quotes the implementer's last line when it has one.

1. Check your background waiter. If it is gone, rearm it: one waiter, never two.
2. Look for a finished helper: mail from `helper`, `revmux` or `human`, `.workbench/state/helpers/*.done`,
   and `.workbench/review/`. Act on the result.
3. If the implementer is at its usage limit (the mail says `The implementer's pane shows a
   usage-limit error: ...`, or its pane shows one): in an `onLimit=wait` checkout run
   `wb.py wait-limit --reason "<the limit line>"` and never report blocked (see "Usage limits"; a
   reason naming any other limit needs `--needs-human` there). Otherwise
   fail over as that section says. In a wait checkout the relay does not send this pointer for a Kimi
   implementer at its limit: it starts the wait itself.
4. Continue the loop. If it really waits on the human, say so in one line and run
   `wb.py status blocked --sound` (in queue mode, also `wb.py loop-state blocked --reason ...`).

`wb.py status blocked` records `.workbench/state/waiting.json`. That record is a latch: while it
exists the stall watch is off. So when the human answers and you resume, run `wb.py status active`,
which removes it. `loop-state resumed` and `loop-state done` remove it too. With no progress for two
more periods after the pointer (no commit, no mail, no helper, no loop report), the relay reports
the loop blocked itself. It sets the blocked sound status, writes waiting.json (`"by": "relay"`), and in queue
mode reports `loop-state blocked` with a reason starting `stalled:`. To continue after that, run
`wb.py status active`, plus `wb.py loop-state resumed` in queue mode.

## Exited agents (the relay's `exit` mail)

When an agent's process exits or crashes and its pane falls back to the pwsh prompt, the relay
restarts it. It waits until the pane has been unchanged for 2 minutes, then types the pane's pinned
restore command, which resumes the conversation, and logs `agent exited; restarted with resume`.
Once the agent's composer is up, it types one `Chat from Workbench:` pointer saying so. A restarted
Claude may therefore get a second, short "continue" turn after its own resume turn. You need to do
nothing for a restart; mail held for the dead pane is rung once its agent is back.

The relay restarts a pane at most 3 times an hour. When it gives up, or the pane has no pin, it
notifies the human on your pane and, when the dead pane is the implementer's, mails you from
`relay`, kind `exit`. Only giving up also sets the blocked sound status, writes waiting.json and,
in queue mode, reports `loop-state blocked` with cause `environment`. Do not restart the agent yourself. Say in one line that the implementer keeps
exiting, and leave it to the human. Once the human has restarted it with the pane's pinned command,
run `wb.py status active` (plus `wb.py loop-state resumed` in queue mode). The human turns
restarting off with `restartExited: false` in `~/.agworkbench.json`.

## Adopted session

If the launcher printed `WORKBENCH ADOPTED`, this existing Claude session now owns that issue.
Use its printed `context:` line as the prefix of **every shell command** for the rest of this
loop: `<context line> && <command>`, including the background `wb.py wait-mail` command. It sources
the generated `adopted.sh`, changes to the issue checkout, and sets `AGWORKBENCH`, `AI_HUB` and
`AI_BOX=claude`. These values replace any inherited context, even if `AI_HUB` was already set.
If sourcing or changing directory fails, fix the context before running work commands; never
continue in the old checkout. Continue this command yourself with the printed issue reference;
the human does not need to type a slash command into your composer.

Shell context does not change the project root used by Read, Write, Edit, Grep, or other file
tools. For **every file read or written for this loop**, use an **absolute path under the printed
`checkout:` directory**, never a relative path. This includes `.workbench/issue.md`, `plan.md`,
scope and PR body files, and all source files inspected or reviewed. Apply the same rule to file
paths in shell commands; do not let the original project root select the old checkout.

## Queue mode

When `.workbench/state/queue-member.json` exists, this issue belongs to an unattended queue.
Report phase changes with `python "$AGWORKBENCH/lib/wb.py" loop-state`; it writes this checkout's
durable report for the conductor. Never edit the global queue file or start another issue yourself.
When a human answers a blocked issue, run `wb.py loop-state resumed` before continuing. Keep the
same conversation and the one-background-waiter rule. A PR is the queue's handoff, not permission
to merge. The conductor admits the next issue while this session continues handling its own review.

A block the human cannot answer - a usage limit you could not fail over, low disk or low memory -
is **environmental**: report it with `wb.py loop-state blocked --environmental --reason "..."` (in an
`onLimit=wait` checkout an agent's usage limit is never blocked: run `wb.py wait-limit`, see Usage limits;
any other reason that mentions a limit needs `--needs-human`). The
member keeps its queue slot, so the conductor does not start another issue on the same broken
tool. A question for the human is a plain `loop-state blocked` and frees the slot.

## The channel

Everything goes through the workbench mailbox (`$AI_HUB`, the `.workbench/` folder of this clone).
You never type into Codex's pane and Codex never types into yours: the relay does the ringing.

```bash
python "$AGWORKBENCH/lib/agmsg.py" send --to codex --kind review-request --subject "plan v1 for #N" --body-file .workbench/plan.md
python "$AGWORKBENCH/lib/agmsg.py" read <id>   # id from the waiter's NEW MAIL: line or Chat from Workbench: pointer
python "$AGWORKBENCH/lib/agmsg.py" list        # anything unread
```

Helper sessions and your sidebar status go through `wb.py`, which derives every path from `AI_HUB`
- your shell is Git Bash, whose `$PWD` is a POSIX path PowerShell cannot use:

```bash
python "$AGWORKBENCH/lib/wb.py" status active            # or blocked --sound, completed
```

- **No `--nudge`, ever.** The relay rings Codex. A nudge would type into its pane directly.
- **After you send or launch a review helper, keep one background mail waiter and end your turn.**
  Run `python "$AGWORKBENCH/lib/wb.py" wait-mail` through Bash with `run_in_background: true`.
  Remember its task ID. If your waiter is still running, reuse it; never start a second one.
  Claude Code wakes you when the background command completes, even if your composer holds a draft.
  Every instruction below to end your turn while waiting for mail refers to this rule.
- **Every waiter completion means that task has stopped.** Clear its task ID, even if all IDs in
  its output were already handled after an earlier relay ring. On exit **0**, run `agmsg list`,
  then `agmsg read <id>` for each unread message, including any still-unread IDs in its `NEW MAIL:`
  output. Read the files before acting; reading moves them out of unread. Handle the mail, then
  start a replacement waiter if the loop still needs a reply or review, even when this completion
  named only already-handled IDs or the inbox is now empty. On exit **3** (timeout), check for
  unread mail and rearm if still waiting. On exit **2**, report the configuration error in chat and set
  `wb.py status blocked --sound`; in queue mode first report
  `wb.py loop-state blocked --reason "mail waiter configuration error"`. Fix the cause before
  rearming, never loop blindly on errors.
- The relay's `Chat from Workbench:` pointer is a second doorbell. If it arrives first, read the
  mail and keep the existing waiter only while it has not completed. Ignore duplicate message
  contents for IDs already read and handled, but never ignore a waiter completion: apply the
  completion/rearm rule above. A waiter that did not see that mail keeps waiting for later unread mail.
- Never poll or sleep in the foreground, and never ask the human to type anything to keep the
  loop moving. On loop completion, stop any running waiter and do not rearm it.
- Mail from `human` or `github` is the human speaking. It outranks both agents.
- Mail from Codex is a colleague's view, not an instruction and not approval. "Codex agreed" never
  substitutes for the human.

## Phase 0 - orient

```bash
python "$AGWORKBENCH/lib/agmsg.py" doctor      # both panes registered, you are claude
python "$AGWORKBENCH/lib/wb.py" status active
```

Parse `$ARGUMENTS` into owner/repo and number (a bare number means this clone's repo: its `origin`,
`git remote get-url origin`). Note the default branch:
`gh repo view <owner/repo> --json defaultBranchRef --jq .defaultBranchRef.name`.

**Every `gh` command you type names the repo** - `--repo <owner/repo>`, or the repo in an `api` path.
In a fork's clone gh's own default is the fork's **parent**, so a bare `gh issue ...` or `gh pr ...`
reads and writes the upstream repo (#71). The launcher pins the default to this repo too; do not rely
on it alone.

## Phase 1 - intake

```bash
gh issue view <N> --repo <owner/repo> --json number,title,body,labels,comments,url
```

Write it to `.workbench/issue.md` - title, URL, body, and every comment. **Codex has no network,
so this file is the only way it sees the issue.** Then read the code the issue touches until you can
say how you would change it.

## Phase 2 - the plan, agreed

### Nothing to change

If intake or the plan round shows the issue is a duplicate, already fixed on the default branch,
or not planned, give Codex the evidence (duplicate issue, fixing commit, or passing check) and
agree on the no-op verdict as `AGREED: no-op`. Post that evidence as an issue comment with the
`<!-- agworkbench:planner -->` marker. With `autonomous=true`, close the issue with the matching
GitHub reason (`gh issue close <N> --repo <owner/repo> --reason "not planned"` for a duplicate or declined work;
`--reason "completed"` when already fixed). Without autonomy, report
`loop-state blocked --reason "no-op: <evidence>; close the issue to finish"` in queue mode, then
set `wb.py status blocked --sound` and wait for the human to decide and close it. Once GitHub
reports CLOSED, run `wb.py status active` and then
`python "$AGWORKBENCH/lib/wb.py" loop-state done --no-pr --reason "<why>"` as the last act,
including in queue mode. A blocked queue member can report `done --no-pr` directly; a
`loop-state resumed` report is not needed. Do not report `loop-state blocked` for a closed no-op issue.

Write `.workbench/plan.md`:

- **Goal** - one paragraph, in the issue's terms.
- **Acceptance criteria** - checkable, each one a thing a test or a command can show.
- **Approach** - what changes and why this way; the alternative you rejected and why.
- **Files** - what will be touched.
- **Tests** - which tests are added or changed, and the exact command that runs them.
- **Out of scope** - what this deliberately does not do.
- **Open questions** - anything you could not decide from the code and the issue.

### When the implementer is Kimi

When `wb.py settings` says `implementer=kimi`, you do the hard thinking and Kimi follows the plan
literally (#77). The owner's evaluation found Kimi's gaps in exactly the places a plan leaves open:
an edit that reached too far (#616 lost edits on save), an invented file format (#309), a wrong field
decoded strictly while the real-file tests were skipped (#701). So the plan also has these sections,
headed exactly so:

- **Exact edits** - the files and functions to change, each with the **scope of the edit** ("guard
  only the `splice_worksheet` call, not the rest of the per-sheet loop").
- **Must not change** - what the edits must leave alone, listed explicitly.
- **Oracle** - the fixture, reference file or sibling code that defines correct behaviour, **by
  path**. The path must exist in the checkout: a URL or an outside spec is never an oracle (plan-check
  counts only what exists). If the repo has none, the plan says `STOP AND REPORT` and why, instead of an oracle: Kimi
  must never invent a format or a sample. **The verdict is a line of its own that starts with
  `STOP AND REPORT`** (a list bullet or bold around it is fine), or the Oracle section's first line.
  The marker inside a sentence, such as a rule restated in Pitfalls, is not a verdict: a restated rule
  never opens its line with the marker, and `STOP AND REPORT when/if ...` is a condition, not a verdict.
- **Tests first** - named tests to write before the fix. Each must fail on the current code, and at
  least one tests a **side effect** ("an edit to a macro sheet survives save").
- **Pitfalls** - the known traps in the area: save paths, undo grouping, unit conversions, shared code
  other features use.
- **Done means** - the exact commands (`cargo fmt`, `cargo clippy -p <crate> -- -D warnings`,
  `cargo test -p <crate>`) and the report Kimi sends: `IMPLEMENTED <sha>` with the test names and
  counts, and **every skipped test by name**.

And these rules, which the plan states where they apply:
- A fix in a reader or a writer runs the real-file oracle (the corpus) when one exists. If it cannot
  run it, the work is not done.
- Any new decoding of an outside binary format is lenient: an unknown value falls back to the old
  behaviour, never to a new hard error.
- Every field id and offset cites its source in the repo (a fixture, a spec or an oracle). Without
  one, the plan says `STOP AND REPORT`.

Before sending each plan version, check it:

```bash
python "$AGWORKBENCH/lib/wb.py" plan-check
```

Exit 1 names what is missing; fix the plan, do not send it. Kimi's critique round stays: it may point
out errors in the plan. A plan that says `STOP AND REPORT` is never implemented as a guess: agree it
as `AGREED: no-op` (see "Nothing to change") with the missing oracle as the evidence, or, when the
human could supply it, report `loop-state blocked --reason "<what is missing>"` in queue mode and
set `wb.py status blocked --sound`.

Send it as `plan v1` (kind `review-request`), keep the background waiter, and end your turn.
Codex answers with a critique. Revise
into `plan v2`, `v3`: quote what Codex said before answering it, concede what is right, argue what
is not, and verify any claim it makes about the code yourself before accepting it.

Agreement is explicit: Codex's reply begins with `AGREED: plan vK`. No agreement after four rounds
means the disagreement belongs to the human: state both positions in chat, set
`python "$AGWORKBENCH/lib/wb.py" status blocked --sound`, and wait.
In queue mode, before ending that turn, run `wb.py loop-state blocked --reason "plan disagreement"`.

An open question only the human can answer goes to the human now, not after implementation.
In queue mode report `wb.py loop-state blocked --reason "<the question>"` before waiting for the answer.

When agreed, send the go-ahead (kind `task`, subject `IMPLEMENT plan vK`), keep the background
waiter, and end your turn.

## Phase 3 - implementation (Codex)

Codex implements on this clone's `issue-*` branch, commits (never commit its uncommitted work
yourself - see "When the implementer is Claude"), runs the tests, and replies
`IMPLEMENTED <sha>` with what it did, what it ran, and anything it did not do. Read the diff
yourself before reviewing it: `git log --oneline origin/<default>..HEAD` and
`git diff origin/<default>...HEAD`.

## Phase 4 - review with revmux, then fix (repeat)

0. **Kimi only** (`implementer=kimi`, #77): before each revmux round, compare the diff with the plan's
   Exact edits and Must not change yourself. Judge the code, not Kimi's report: a green run and an
   accurate list of commands have hidden real gaps before. Any edit beyond its stated scope, any
   change to a must-not-change item, a test that skips instead of running the oracle, or a strict new
   decode goes back as a fix round (`FIX r<K>` with the evidence) before revmux runs. Never merge on
   Kimi's own report.
1. Write `.workbench/review/scope-r<K>.md`: what the change is for (link the plan), the diff range
   `origin/<default>..HEAD`, the acceptance criteria, where to look hardest, and what is out of
   scope. Tell reviewers **not** to run interactive or GUI tests.
2. Launch revmux in its own **visible** session - never a hidden background process:

   ```bash
   python "$AGWORKBENCH/lib/wb.py" revmux --round <K> --scope .workbench/review/scope-r<K>.md
   ```

   Keep the background waiter and end your turn. The report is posted to you as mail from
   `revmux` when it finishes (3-20 minutes).
3. Read it. Exit 1 means findings, not failure. Check the sources line: a degraded run is a partial
   review and is never reported as clean. **Verify every finding against the code yourself** before
   passing it on; a finding you cannot reproduce is dropped with the reason, not forwarded. On this
   machine, a finding that rests on *reading* a file rather than running it gets its bytes checked -
   two past review rounds reported the same non-existent defect by reading through a lossy console.
4. Record the round's decision (#64) before you send anything:

   ```bash
   python "$AGWORKBENCH/lib/wb.py" review-round --round <K>
   ```

   It reads `.workbench/review/revmux-r<K>.md` and counts the Blocker, Critical and Major findings.
   When one of them did not verify (you could not reproduce it, or it is really a Minor), pass the
   verified count with the reason: `--severe <n> --reason "<why>"`. Never go below revmux's count
   without a reason. The difference counts as Minor findings, so the round stops rather than reads
   clean. Exit 2 means the report is incomplete or the config is invalid: look, do not guess.
   It prints one decision:

   | decision | when | next |
   |---|---|---|
   | `continue` | a verified Major+, a degraded run, or `review.stopWhenNoMajor` is false or `review.minRounds` is not reached | `FIX r<K>`, then round K+1 |
   | `stop` | no verified Major+ (and at or past `minRounds`) | `FIX r<K> (final)`: no revmux round after it |
   | `clean` | no findings at all | review is done |
   | `cap` | a round at or past the review cap still has a Major+ or is degraded | `FIX r<K>`, then the human (with `stopWhenNoMajor: false`, today's rule: the fixes verified, as condition 1 says) |
   | `limit` | `onLimit=wait` and a reviewer was degraded by its usage limit (#77) | no FIX: rerun the same round as the output says, then record it again |

   **`limit`** is no review at all, so it is neither clean nor degraded and never counts toward the
   cap. Send no findings from it. Run exactly the command it prints:
   `wb.py revmux --round <K> --rerun --after <minutes>` (`reviewOnLimit: "wait"`, the default: the
   `#N revmux r<K>` session counts down on screen, then reviews) or
   `wb.py revmux --round <K> --rerun --profile claude-only` (`reviewOnLimit: "fallback"`). Keep your
   waiter and end your turn; when the rerun's report arrives, run `review-round --round <K>` again.
   merge-check refuses the loop until you do.

   **The review cap** is `review.maxRounds` (5), or `review.maxRoundsBig` (10) for a **big** issue: a
   title starting with `Batch:`, a `batch` or `big` label, a diff past `review.bigDiffLines` (1500
   lines added + deleted against the base) when a round is recorded, or a checkout launched with
   `-BigReview` (#75). Judged big once, an issue stays big. `review-round` prints the cap it used
   (`cap=10 (big: <why>)`), and `wb.py settings` prints `reviewCap=<n>` with the same reason.

5. Send the verified findings to Codex (kind `review`, subject `FIX r<K>`, or `FIX r<K> (final)`
   after a `stop`): one block per finding with file:line, the failure it causes, and your evidence.
   Keep the background waiter and end your turn.
6. Codex answers `FIXED <sha>` with each finding marked fixed, disputed (with evidence), or deferred
   (with a reason). Silence on a finding is not an answer. Check the fixes; argue the disputes.
   After a final round, record each deferred Minor or Immaterial finding with
   `follow-up add ... --origin "review r<K>"` (see "Follow-ups"). **Review stops once a round has no
   Major**: its minors are fixed if cheap and recorded otherwise, and no further revmux round runs.

A round with a Major gets another round after its fix. At most the review cap's revmux rounds (five,
or ten for a big issue); after that, what is left goes to the human with both positions. A deferred finding is always a recorded and filed
follow-up (see "Follow-ups").
In queue mode, report `wb.py loop-state blocked --reason "review rounds exhausted: <remaining issue>"`
before ending the turn to wait for the human.

## Phase 5 - the pull request

Codex cannot push. You do:

```bash
git push -u origin HEAD
gh pr create --repo <owner/repo> --base <default> --title "<title>" --body-file .workbench/pr-body.md
```

The body: what changed and why, `Closes #<N>`, how it was tested, and the review record - rounds
run, findings fixed, findings disputed and why, and the line `wb.py review-round --summary` prints
(why review ended; leave it out when it exits 2, meaning no revmux round was recorded).
It ends with the planner marker line. The relay notices the PR on its next check and watches it from then on.

**A stop's deferred minors are filed now**, with or without auto-merge - nothing later files them
when the human merges: right after `gh pr create`, run
`python "$AGWORKBENCH/lib/wb.py" follow-up file --source <N> --pr <P>`, run `review-round --summary`
again, put its line (now with the leftovers URL) in `.workbench/pr-body.md`, and
`gh pr edit <P> --repo <owner/repo> --body-file .workbench/pr-body.md`.

## Phase 6 - the human's review

### Auto-merge (only when this checkout has it on)

`python "$AGWORKBENCH/lib/wb.py" settings` prints `autoMerge=true` only when the human turned it on
(`-AutoMerge` or `"autoMerge": true`). Otherwise skip this section: merging is the human's. With it
on, you merge only when **all** of these hold:

1. **The review is clean.** The last revmux round's findings are all fixed and verified, or disputed
   with evidence. None is deferred, and it is within the review cap. A review that **stopped**
   (`review-round` decided `stop`: that round had no Major) is clean too, when each of its findings is
   fixed or recorded as a follow-up; file the recorded ones (`follow-up file --source <N> --pr <P>`)
   before merge-check, with or without autonomy. With full autonomy, a
   remaining Minor or Immaterial finding may also be deferred **with a filed follow-up issue**; a
   Major or blocker never may, disputed or not. A round that ended with open findings goes to the
   human instead. merge-check refuses a last round that decided `continue`; a `cap` (a Major or a
   degraded run at or past the review cap) unless it was recorded with `stopWhenNoMajor: false`; and the
   newest revmux report when it has no recorded decision.
2. **The whole suite passed on the PR head.** Note that commit's full SHA (`git rev-parse HEAD`
   after the push) and the test count. Run it with `wb.py suite --label <sha7> -- <command>` (see
   Rules). The result arrives as mail from `helper`.
3. **merge-check says `ok`.** It checks, read-only: the PR is OPEN, MERGEABLE and CLEAN; no review
   requests changes; no hold label, title, description, comment, review or line comment that you
   did not mark (`hold`, `wait`, `waiting`, `wip`, `do not merge` and their spellings; a hold is
   lifted only by its own author, later, with a comment that is just `go ahead`, `resume` or
   `unhold`); you have no unread mail from `human` or `github`; the relay has seen the PR open; and
   the PR head is the tested SHA.

   **Wait for the relay first.** After opening the PR, keep the background waiter and end your turn
   until the relay's `PR #N is open` mail from `github` arrives. Read it, and any other unread mail,
   and only then run:

   ```bash
   python "$AGWORKBENCH/lib/wb.py" merge-check --pr <N> --head <full sha>
   ```

   **Retryable failures:** `relay:` (wait for the relay's mail), `mail:` (read and handle the mail),
   and a merge state of `UNKNOWN` ("retry in ~30s", run once more after about 30 seconds). Handle
   them, then check again.

   **Routed failures (#32)** - the ordinary reasons a clean-reviewed PR is not mergeable yet. Only
   here, in the auto-merge branch; without auto-merge they are reported like any other failure.

   | merge-check line | what you do |
   |---|---|
   | `ci-pending:` | Any check still running, required or optional (merge-check reports nothing else about CI until all are done). End your turn: the relay's `ci` mail for this head (see "CI results") wakes you when CI finishes, and you run merge-check again. You may also start `python "$AGWORKBENCH/lib/wb.py" wait-ci --pr <N> --head <full sha>` in the background (it is not a mail waiter; keep your one mail waiter too); whichever comes first counts, so stop the other. When wait-ci ends: exit 0 (`CI DONE`) - run merge-check again; exit 4 (head changed / PR not open) - stop and look; exit 3 (timeout) - the human's. A wait-ci that was **killed** (low memory) means "rerun merge-check", never "CI done". |
   | `ci-failed:` | Reported only once nothing is running. A GitHub Actions check: `wb.py ci-rerun --pr <N>` (once; counted only when a rerun started). Exit 0: wait for the relay's next `ci` mail (or wait-ci), then merge-check. Exit 2 is operational: retry ci-rerun, or, when it says `rerun started`, wait as after exit 0. Still `ci-failed:`, or ci-rerun exits 1 (external CI, or the rerun is used): `wb.py merge-round --pr <N> --kind ci-fix`, then `wb.py ci-log --pr <N>` (exit 2: no job log could be fetched yet - retry it) and a `FIX r<K>` round whose evidence is the log file it wrote; after the fix, the whole suite, push, wait for the relay's `ci` mail for the new head (or wait-ci), merge-check. Still red, or merge-round refuses: the human's. |
   | `behind:` / `conflict:` | An UPDATE round, below. |
   | `ci-optional-failed:`, `head:`, `state:`, `mergeable:`, and every other line | Final for this head: the human's. |

   **An UPDATE round.** Run `git fetch origin` (the implementer may have no network) and note
   `git rev-parse origin/<default>` - the base SHA. Mail the implementer `UPDATE <default> <base sha>`:
   merge exactly that SHA into the issue branch with `git merge --no-ff <base sha>` (never `git pull`,
   **never rebase, never force-push**), resolve any conflicts, run the whole suite, and reply
   `UPDATED <sha>` or `CANNOT-RESOLVE <why>`. On `UPDATED`, prove it before anything else:

   ```bash
   python "$AGWORKBENCH/lib/wb.py" update-check --reviewed <reviewed head sha> --base <base sha>
   ```

   It refuses anything but one merge commit of that base onto the reviewed head, with a clean tree
   (exit 1: the human's). It prints `update: clean` (git's own merge, nothing added) or
   `update: conflict` (a non-empty `git show --remerge-diff`: read that diff like a fix - resolved
   conflicts and anything else added in the merge). A conflict is `(small: ...; not counted)` or
   `(counted: <reason>)` (#90). It is small when it has at most `mergeRounds.smallConflictHunks`
   conflict hunks (3), changes nothing outside them, and touches no file the review flagged. Then count
   it: `wb.py merge-round --pr <N> --kind update` for clean, `--kind conflict` for conflict.
   merge-round reads update-check's record for the HEAD, so a small conflict is never counted and you
   never choose that yourself. A kind that contradicts the record exits 2. A refusal (the fourth clean
   catch-up, or a fourth counted conflict; a small conflict is never refused) or `CANNOT-RESOLVE` goes
   to the human. On a refusal, say **blocked** in the PR comment and in chat, with merge-round's
   refusal line verbatim. Push with
   a plain `git push` (never `--force` or `--force-with-lease`; a rejected push means the remote
   moved - a new round), then wait for the relay's `ci` mail for the new head (or wait-ci), and
   merge-check with the new head.

   Every other failure is final for this head.

   **Check again after any event that could change the verdict**, such as the hold's author lifting
   it, or a fix round pushing a new head with the whole suite re-run on it. Run the auto-merge check
   for the new head.

On `ok`, merge exactly that commit, then say so in the PR and in chat:

```bash
gh pr merge <N> --repo <owner/repo> --merge --delete-branch --match-head-commit <full sha>
gh pr comment <N> --repo <owner/repo> --body-file .workbench/merge-note.md
```

The note states each condition as a checked fact: "Merged automatically (auto-merge is on for this
checkout): <the `wb.py review-round --summary` line>; <the `wb.py merge-round --pr <N> --summary`
line>; whole suite green on <sha> (<count> tests); merge-check ok." That line is `review clean after round K`, or `review stopped: round K had no Major;
N minor finding(s) in <leftovers URL>` once the follow-ups are filed. The merge-round line says each
UPDATE round's decision, for example `merge rounds: 1 clean update; conflicts: 2 small (uncounted), 1
counted of 3` (#90). It ends with the planner marker line. The relay then reports the merge, and Phase 7
runs as for a human merge. In queue mode, report `loop-state pr-open` first, as below.

If any condition fails, do not merge. Post merge-check's failure lines (or which of conditions 1-2
failed) verbatim on the PR, with the marker, repeat them in chat, and continue with the human's
review below.

### The human's review

In queue mode, run `python "$AGWORKBENCH/lib/wb.py" loop-state pr-open --pr <url>` before ending
the turn, set `wb.py status idle`, and keep the background waiter. Do not open revdiff automatically.
Tell the human that `wb.py human-review --base origin/<default>` opens it on demand, or they can
review on GitHub. The conductor can start the next issue immediately; continue handling this PR's
feedback below in this session.

Outside queue mode, open revdiff for the human in its own session, **selected**:

```bash
python "$AGWORKBENCH/lib/wb.py" human-review --base origin/<default>
```

Outside queue mode, tell them where it is and that reviewing on GitHub works just as well. Then set
`python "$AGWORKBENCH/lib/wb.py" status blocked --sound`, keep the background waiter, and end your turn.

Their feedback reaches you as mail - from `human` (revdiff annotations) or from `github` (PR
reviews, comments, line comments, the review decision). For each round of it:

1. Interpret it; if an annotation is a question (`??`, "why", "explain"), answer it on the PR with
   `gh pr comment` rather than turning it into code.
2. Send the change requests to Codex as a fix round. Review the fix - a direct diff read for small
   changes, another revmux round for substantial ones (recorded with `review-round` like any
   other, rounds past the review cap included, or merge-check refuses its report; a Major at or past
   the cap is a `cap`).
3. Push, and reply on the PR saying what changed for each point.

## Phase 7 - done

When mail arrives saying the PR was **MERGED**: post a short summary in chat (what shipped, rounds,
anything deferred), mail Codex that the loop is complete, run
`python "$AGWORKBENCH/lib/wb.py" status completed`, stop any running background waiter by its task ID,
and stop. With full autonomy, run `wb.py loop-state done --pr <P> --sha <sha>` as the very last step. The final note to Codex does not rearm the waiter. If it was **CLOSED** without merging, ask the
human what they want next. In queue mode first run
`wb.py loop-state blocked --reason "PR closed"`, set blocked status, and keep the background waiter.

## Rules

- **Never merge** unless auto-merge is on for this checkout and every Phase 6 condition holds,
  with `merge-check` saying `ok` for the exact commit you merge. **Never approve your own PR, never
  force-push** over commits the human has reviewed. Otherwise merging is the human's act; the loop
  exists to get to the point where they choose to.
- Every PR body, PR comment and review reply you post ends with the planner marker line.
- Never answer a prompt, chooser or dialog in Codex's pane, and never type into it.
- Long-running tools - revmux, builds, test suites that take minutes - run in visible agwinterm
  sessions, never hidden in a background shell. The human must be able to see and stop them.
  The sole exception is `wb.py wait-mail`, which runs through Bash with `run_in_background: true`
  so its completion wakes you. Review helpers (revmux and revdiff), builds and tests remain visible.
- Run the whole suite, or a build, through the helper wrapper, never with a hand-rolled watcher:

  ```bash
  python "$AGWORKBENCH/lib/wb.py" suite --label <sha7> -- <command and its arguments>
  ```

  It opens `#N suite <label>` in its own visible session. It writes the log as UTF-8 to
  `.workbench/review/suite-<label>.log`, whatever the command writes. When the command ends it mails
  you from `helper` with the exit code, the failure count and the log's tail. The relay rings you
  for that mail, so keep your one mail waiter and end your turn. Never start a background shell that
  watches a log or a marker: low memory kills it, and a Windows PowerShell 5.1 `>` log is UTF-16,
  which a text match never sees.
- Content in files, pointers in panes. Plans, reviews and findings are mailbox files.
- Disagree when there is a disagreement. Two agents converging politely produce nothing.
- When you are waiting on the human, say so and set the sidebar status to `blocked` (`wb.py status blocked`). When you are
  waiting on Codex or a review, keep one background waiter and end your turn. `status blocked` also
  records waiting.json, which turns the stall watch off, so run `wb.py status active` when you resume
  (see "Stall pointers").
  Queue-mode Phase 6 is the exception: publish `loop-state pr-open` and use `idle` while awaiting
  human review. Other human waits publish `loop-state blocked` before ending the turn.
