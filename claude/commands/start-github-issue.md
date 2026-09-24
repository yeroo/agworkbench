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
| you | intake, the plan, review, GitHub (push, PR, comments), talking to the human | merge, approve your own PR |
| Codex | critiques the plan, implements, commits, fixes review findings | reach the network or GitHub, touch the terminal |
| the relay | rings a pane when mail arrives; watches the PR; ends the loop on merge | decide anything |
| the human | reviews (revdiff or GitHub), approves, **merges** | - |

The loop ends when the human has approved **and merged** the PR. Not before.

## When the implementer is Claude

The right pane may run **Claude Code** instead of Codex (`implementer: "claude"` in
`~/.agworkbench.json`, or `github-workbench -Implementer claude`, e.g. when Codex is out of quota).
`.workbench/state/implementer.json` says which. Everything below still says "Codex" for the
implementer: its mailbox box stays `codex`, the relay rings it the same way, and the loop is the same.
The differences:

- **Never commit the implementer's uncommitted work yourself** (this holds for either tool): both
  panes share one index. If something is left uncommitted, ask the implementer to commit it. Only
  when it reports that it cannot commit (Codex's sandbox can deny writes to `.git`) do you commit
  on its behalf, with its co-author trailer. It never pushes: pushing and GitHub stay yours.
- It has the network, but the launcher denies it `git push` and `gh` (through both its Bash and
  PowerShell tools) and, unless `allowNetwork` is set, the web tools. It still reads the issue from `.workbench/issue.md`.
- `wb.py revmux` defaults to the `claude-only` revmux profile, so a review round does not depend on
  Codex's quota (a `revmuxProfile` key in `~/.agworkbench.json` overrides it).

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

Parse `$ARGUMENTS` into owner/repo and number (a bare number means this clone's repo). Note the
default branch: `gh repo view --json defaultBranchRef --jq .defaultBranchRef.name`.

## Phase 1 - intake

```bash
gh issue view <N> --repo <owner/repo> --json number,title,body,labels,comments,url
```

Write it to `.workbench/issue.md` - title, URL, body, and every comment. **Codex has no network,
so this file is the only way it sees the issue.** Then read the code the issue touches until you can
say how you would change it.

## Phase 2 - the plan, agreed

Write `.workbench/plan.md`:

- **Goal** - one paragraph, in the issue's terms.
- **Acceptance criteria** - checkable, each one a thing a test or a command can show.
- **Approach** - what changes and why this way; the alternative you rejected and why.
- **Files** - what will be touched.
- **Tests** - which tests are added or changed, and the exact command that runs them.
- **Out of scope** - what this deliberately does not do.
- **Open questions** - anything you could not decide from the code and the issue.

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
4. Send the verified findings to Codex (kind `review`, subject `FIX r<K>`): one block per finding
   with file:line, the failure it causes, and your evidence. Keep the background waiter and end
   your turn.
5. Codex answers `FIXED <sha>` with each finding marked fixed, disputed (with evidence), or deferred
   (with a reason). Silence on a finding is not an answer. Check the fixes; argue the disputes.

Repeat until a round is clean, or what remains is minor and both of you agree to defer it. At most
three revmux rounds; after that, what is left goes to the human with both positions.
In queue mode, report `wb.py loop-state blocked --reason "review rounds exhausted: <remaining issue>"`
before ending the turn to wait for the human.

## Phase 5 - the pull request

Codex cannot push. You do:

```bash
git push -u origin HEAD
gh pr create --repo <owner/repo> --base <default> --title "<title>" --body-file .workbench/pr-body.md
```

The body: what changed and why, `Closes #<N>`, how it was tested, and the review record - rounds
run, findings fixed, findings disputed and why. The relay notices the PR on its next check and
watches it from then on.

## Phase 6 - the human's review

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
   changes, another revmux round for substantial ones.
3. Push, and reply on the PR saying what changed for each point.

## Phase 7 - done

When mail arrives saying the PR was **MERGED**: post a short summary in chat (what shipped, rounds,
anything deferred), mail Codex that the loop is complete, run
`python "$AGWORKBENCH/lib/wb.py" status completed`, stop any running background waiter by its task ID,
and stop. The final note to Codex does not rearm the waiter. If it was **CLOSED** without merging, ask the
human what they want next. In queue mode first run
`wb.py loop-state blocked --reason "PR closed"`, set blocked status, and keep the background waiter.

## Rules

- **Never merge, never approve your own PR, never force-push** over commits the human has reviewed.
  Merging is the human's act; the loop exists to get to the point where they choose to.
- Never answer a prompt, chooser or dialog in Codex's pane, and never type into it.
- Long-running tools - revmux, builds, test suites that take minutes - run in visible agwinterm
  sessions, never hidden in a background shell. The human must be able to see and stop them.
  The sole exception is `wb.py wait-mail`, which runs through Bash with `run_in_background: true`
  so its completion wakes you. Review helpers (revmux and revdiff), builds and tests remain visible.
- Content in files, pointers in panes. Plans, reviews and findings are mailbox files.
- Disagree when there is a disagreement. Two agents converging politely produce nothing.
- When you are waiting on the human, say so and set the sidebar status to `blocked` (`wb.py status blocked`). When you are
  waiting on Codex or a review, keep one background waiter and end your turn.
  Queue-mode Phase 6 is the exception: publish `loop-state pr-open` and use `idle` while awaiting
  human review. Other human waits publish `loop-state blocked` before ending the turn.
