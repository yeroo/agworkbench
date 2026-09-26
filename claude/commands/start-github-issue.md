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
| the relay | rings a pane when mail arrives; watches the PR; ends the loop on merge | decide anything |
| the human | reviews (revdiff or GitHub), approves, **merges** | - |

The loop ends when the human has approved **and merged** the PR. Not before. With auto-merge on
for this checkout, you may merge it yourself once every condition in Phase 6 holds.

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

- **Follow-ups are recorded as you go, and filed before the merge.** For every finding you defer, and
  for every "Out of scope" or "Follow-up" item in the agreed plan, record one item:

  ```bash
  python "$AGWORKBENCH/lib/wb.py" follow-up add --key r2-m1 --title "<issue title>" --body-file <evidence.md> \
      --severity minor --origin "review r2"          # add --disputed for a finding that ended disputed
  ```

  The severity is revmux's, as you verified it. You may raise it, but never lower it below revmux's
  without saying so in the merge comment.
  Before merge-check, file them all with `python "$AGWORKBENCH/lib/wb.py" follow-up file --source <N> --pr <P>`.
  It dedupes on the exact title, labels the issue `follow-up` (`follow-up-nested` when this issue is
  itself a follow-up), and adds the planner marker. merge-check refuses while any item is unfiled.
- **What may be deferred.** After at most five rounds, a remaining Minor or Immaterial finding may
  be deferred, but only as a filed follow-up. A Major or blocker **never** may, disputed or not: it
  stops as today. merge-check refuses any Major or blocker review item in follow-ups.json, so record
  it honestly (with `--disputed` when it ended disputed) and the human decides.
- **The merge comment** lists every follow-up URL, and every disputed or deferred finding with its
  severity.
- **Your last act** is `python "$AGWORKBENCH/lib/wb.py" loop-state done --pr <P> --sha <merged sha>`, after the
  Phase 7 steps. It refuses while any follow-up is unfiled. The relay closes nothing until this
  record exists and your own inbox is read. Mail the implementer gets after the merge, such as your
  "loop complete" note, does not have to be read; older unread mail holds the close for 10 minutes
  at most (#44). Then it closes:
  - the helper sessions that are back at a shell;
  - this issue's session;
  - itself.

  It logs every step to `.workbench/state/relay-close.log`, and never closes on a timeout.

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

## Usage limits (the relay's `usage limit:` mail)

The relay watches both panes for an agent's own usage-limit message. When it sees one on two
consecutive reads, it mails you from `relay` with the subject `usage limit: <box> (<tool>) <kind>`.
The mail carries the matched line and the pane's last rows. It also sets that pane blocked, with a
desktop notification. It stops ringing a limited implementer: mail to it waits.

- **The implementer is `limited`, and `wb.py settings` says `failover=true`** (the default):
  1. Check the frame in the mail. The matched line must be the agent's own limit message at the end
     of its pane, not text it printed from a file, a diff or a test.
  2. Fail over. The command may take up to two minutes while it checks that the pane is idle, so
     run it through Bash with `timeout: 600000`. Your shell is Git Bash, which only finds the
     launcher by its full name, `github-workbench.cmd`:

     ```bash
     github-workbench.cmd <owner/repo#N> -Failover
     ```

     If the agent already exited, it switches straight away. If it is still running, the launcher
     stops it only when it is provably idle at its limit (an unchanged pane, no `.git/index.lock`,
     exactly one agent process). It records the limit, clears the pane, starts the other tool
     there, and restarts the relay for it. It never types into the limited agent.
  3. Hand over. Run `python "$AGWORKBENCH/lib/wb.py" handover`, then mail the new implementer
     (kind `task`, subject `HANDOVER`):
     - the current phase and plan version;
     - the open request's id and subject from that output;
     - the branch;
     - "run git status: uncommitted changes are the previous implementer's work - review, finish
       and commit them".
  4. Tell the human in one line which tool was stopped and which took over.
- **`-Failover` refused** (exit 2, `Implementer switch refused: failover refused: ...`): tell the
  human the refusal line and set `wb.py status blocked --sound`. Exit 2 means nothing was stopped
  and nothing changed. A tool with a recorded limit is never switched back to automatically: the
  human clears it with `github-workbench <issue> -Implementer <tool>` once its limit has reset.
- **`-Failover` stopped the agent but did not switch** (exit 3, `Failover incomplete: ...`): the
  limited agent may be gone, and its limit is recorded. Tell the human the line and set blocked. They
  relaunch with `github-workbench <issue> -Implementer <other tool>` once the pane is a clean shell.
- **`warning`** (Codex's "Approaching rate limits" chooser): never answer it. Tell the human in one
  line and set blocked.
- **`failover=false`**: tell the human and set blocked.
- **Box `claude`** (you): this mail is only a record; the human was already notified. Carry on
  when you can act again.

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
five revmux rounds; after that, what is left goes to the human with both positions. With full
autonomy, a deferred finding is always a recorded and filed follow-up (see "Full autonomy").
In queue mode, report `wb.py loop-state blocked --reason "review rounds exhausted: <remaining issue>"`
before ending the turn to wait for the human.

## Phase 5 - the pull request

Codex cannot push. You do:

```bash
git push -u origin HEAD
gh pr create --repo <owner/repo> --base <default> --title "<title>" --body-file .workbench/pr-body.md
```

The body: what changed and why, `Closes #<N>`, how it was tested, and the review record - rounds
run, findings fixed, findings disputed and why. It ends with the planner marker line. The relay notices the PR on its next check and
watches it from then on.

## Phase 6 - the human's review

### Auto-merge (only when this checkout has it on)

`python "$AGWORKBENCH/lib/wb.py" settings` prints `autoMerge=true` only when the human turned it on
(`-AutoMerge` or `"autoMerge": true`). Otherwise skip this section: merging is the human's. With it
on, you merge only when **all** of these hold:

1. **The review is clean.** The last revmux round's findings are all fixed and verified, or disputed
   with evidence. None is deferred, and it is within the five-round cap. With full autonomy, a
   remaining Minor or Immaterial finding may also be deferred **with a filed follow-up issue**; a
   Major or blocker never may, disputed or not. A round that ended with open findings goes to the
   human instead.
2. **The whole suite passed on the PR head.** Note that commit's full SHA (`git rev-parse HEAD`
   after the push) and the test count.
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
   | `ci-pending:` | Any check still running, required or optional (merge-check reports nothing else about CI until all are done). Start `python "$AGWORKBENCH/lib/wb.py" wait-ci --pr <N> --head <full sha>` in the background (it is not a mail waiter; keep your one mail waiter too) and end your turn. When it ends: exit 0 (`CI DONE`) - run merge-check again; exit 4 (head changed / PR not open) - stop and look; exit 3 (timeout) - the human's. A wait-ci that was **killed** (low memory) means "rerun merge-check", never "CI done". |
   | `ci-failed:` | Reported only once nothing is running. A GitHub Actions check: `wb.py ci-rerun --pr <N>` (once; counted only when a rerun started). Exit 0: wait-ci, then merge-check. Exit 2 is operational: retry ci-rerun, or, when it says `rerun started`, run wait-ci. Still `ci-failed:`, or ci-rerun exits 1 (external CI, or the rerun is used): `wb.py merge-round --pr <N> --kind ci-fix`, then `wb.py ci-log --pr <N>` (exit 2: no job log could be fetched yet - retry it) and a `FIX r<K>` round whose evidence is the log file it wrote; after the fix, the whole suite, push, wait-ci, merge-check. Still red, or merge-round refuses: the human's. |
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
   conflicts and anything else added in the merge). Then count it:
   `wb.py merge-round --pr <N> --kind update` for clean, `--kind conflict` for conflict. A refusal
   (the fourth clean catch-up, or a second conflict) or `CANNOT-RESOLVE` goes to the human. Push with
   a plain `git push` (never `--force` or `--force-with-lease`; a rejected push means the remote
   moved - a new round), then wait-ci and merge-check with the new head.

   Every other failure is final for this head.

   **Check again after any event that could change the verdict**, such as the hold's author lifting
   it, or a fix round pushing a new head with the whole suite re-run on it. Run the auto-merge check
   for the new head.

On `ok`, merge exactly that commit, then say so in the PR and in chat:

```bash
gh pr merge <N> --merge --delete-branch --match-head-commit <full sha>
gh pr comment <N> --body-file .workbench/merge-note.md
```

The note states each condition as a checked fact: "Merged automatically (auto-merge is on for this
checkout): review clean after <K> revmux round(s); whole suite green on <sha> (<count> tests);
merge-check ok." It ends with the planner marker line. The relay then reports the merge, and Phase 7
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
   changes, another revmux round for substantial ones.
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
- Content in files, pointers in panes. Plans, reviews and findings are mailbox files.
- Disagree when there is a disagreement. Two agents converging politely produce nothing.
- When you are waiting on the human, say so and set the sidebar status to `blocked` (`wb.py status blocked`). When you are
  waiting on Codex or a review, keep one background waiter and end your turn.
  Queue-mode Phase 6 is the exception: publish `loop-state pr-open` and use `idle` while awaiting
  human review. Other human waits publish `loop-state blocked` before ending the turn.
