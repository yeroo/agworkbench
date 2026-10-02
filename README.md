# agworkbench

**Two agents, one GitHub issue, one terminal window.** Claude Code on the left, Codex on the right.
They agree a plan, Codex implements, Claude reviews, Codex fixes — and it stops when *you* merge
the pull request.

```
github-workbench yeroo/agworkbench#7
```

This is a Windows reproduction of the two-pane workflow umputun runs on macOS with
[agterm](https://github.com/umputun/agterm) — his cookbook recipe
[`two-agent-chat`](https://github.com/umputun/agterm/tree/master/cookbook/two-agent-chat) — built on
[agwinterm](https://github.com/yeroo/agwinterm) and extended into a full issue-to-merge loop with his
review tools [revmux](https://github.com/umputun/revmux) and [revdiff](https://github.com/umputun/revdiff).

## Why two agents

The value is disagreement. In umputun's words: *"An agent working alone accepts its own reasoning;
a second one with its own context attacks it first, and what comes back is a located disagreement
or a checked fact rather than agreement."* Two agents reviewing the same change find different
defects, and each can refute the other's finding before it reaches you.

## Install

```powershell
git clone https://github.com/yeroo/agworkbench
cd agworkbench
.\install.ps1            # asks before installing anything
```

It installs what is missing — git, gh, python, node, go (via scoop); agwinterm (via scoop, from
[yeroo/scoop-bucket](https://github.com/yeroo/scoop-bucket)) with its agent skill and status hooks;
Claude Code; the Codex CLI; revmux and revdiff (via `go install`, as neither ships a Windows binary)
— then the `/start-github-issue` command for Claude, the `workbench-implementer` skill for Codex,
and `github-workbench` on your PATH.

`.\install.ps1 -Bypass` starts Claude with `--dangerously-skip-permissions` in workbench sessions.
Without it, Claude stops to ask before every shell command and the loop stops with it. Codex is
sandboxed either way (below).

## Use

From PowerShell or cmd, anywhere:

```
github-workbench 42                                         # issue 42 of the repo you are in
github-workbench owner/repo#42
github-workbench https://github.com/owner/repo/issues/42
github-workbench owner/repo#42 -DryRun                      # show the plan, touch nothing
github-workbench owner/repo#42 -NewSession                  # separate session, or resume its existing workbench
github-workbench -Version                                  # installed toolchain, one line per tool
```

Inside agwinterm (or agliteterm), the launcher adopts its current session: it keeps the caller as
Claude, splits in Codex, renames the session, and moves it to the repository's workspace. An
already-running Claude's composer is untouched; the launcher prints a context command so that
Claude continues the issue in its clone with the correct mailbox. From a plain shell, Claude
starts there after setup. A foreign split, another issue's session, or a conflicting existing
workbench is refused before setup changes anything. Rerun from the same Claude pane to complete
a partial adoption. `-NewSession` uses the separate-session/resume behavior. From any other
terminal, the launcher starts agwinterm — installing it with scoop first if needed — and opens
the session there.

In PowerShell, a bare `#42` is a comment. Type `42`, or quote it.

## What happens

Each launch appends timestamped steps, terminal commands, and failures to
`<checkout>/.workbench/state/launch.log`. If setup fails, the launcher prints the known session
and pane IDs, commands that can be run by hand, and the command to resume. Failures before the
checkout exists print the buffered log in the launching shell.

Run the same issue command again to complete an existing session: register its panes, start
Codex when its pane shows a recognized empty shell prompt, and start or resume its relay. If only
Codex's pane survives, the new split starts Claude. A relay using replaced panes is asked to stop
before it is restarted with the current pane IDs; if its shell cannot be confirmed, setup stops
with repair instructions. An
unrecognized pane is left alone and the launcher prints the manual launch command. It uses the
checkout's registered Claude pane to locate the session; the fallback matches the repository's
workspace name and issue number/title. That fallback cannot distinguish owners with identical
repository names. Ambiguous matches are reported with their session IDs rather than chosen.

The launcher pins restart commands for Claude, Codex and the relay. Pinned commands always replay
on restart. A failed pin stops setup with repair instructions.
Review helpers are not pinned. Restarting agwinterm with its session tree intact replays these
commands; running the launcher again refreshes the pins and leaves non-shell panes untouched.

To work through several issues without waiting between them, run inside agwinterm:

```powershell
github-workbench -Queue 'owner/repo#3,#4,#5'
github-workbench -Queue '3,4,5' -Repo owner/repo -Parallel 2
github-workbench -Queue 'label:workbench' -Repo owner/repo -Watch
github-workbench -Queue 'owner/repo#3,#4,#5' -Retry
github-workbench -Queue bugs -Repo yeroo/docxy -Autonomous
```

**`-Queue bugs`** is `label:<bugLabel>`, with `bugLabel` in `~/.agworkbench.json`, default `bug`.

**`-Queue 'where: <query>'`** selects open issues with a boolean label query (#38):

```powershell
github-workbench -Queue 'where: bug AND priority IN [P0, P1] AND NOT wontfix' -Repo yeroo/docxy -Autonomous -Triage
github-workbench -Queue 'where: (bug OR follow-up) AND priority NOT IN [P2, P3]' -Repo yeroo/docxy
github-workbench -Queue "where: label IN [bug, regression] AND NOT 'needs design'" -Repo yeroo/docxy
```

- **Operators:** `NOT` binds tighter than `AND`, which binds tighter than `OR`. Parentheses
  override that: `bug AND (priority IN [P0] OR ux)` is not `bug AND priority IN [P0] OR ux` (the
  second also takes every `ux` issue). There is no implicit AND: `bug wontfix` is an error.
- **Membership:** `KEY IN [a, b]` means the issue has the label `KEY:a` or `KEY:b`, exactly and
  case-insensitively, which fits `priority:P0`. The key `label` means the bare names:
  `label IN [bug, regression]` is `bug OR regression`. `NOT IN` is the negation, so it also takes
  issues with no such label at all: `priority NOT IN [P2, P3]` includes the untriaged ones, and an
  unknown key under `NOT IN` matches everything.
- **Labels:** a bare label is letters, digits and `: - _ . /`. Anything else is quoted with `"..."`
  or `'...'`, and a backslash escapes the next character: `"good first issue"`, `'c++'`, `"🐛"`. A
  label spelled `and`, `or`, `not` or `in` must be quoted too. Keywords and labels match in any
  case.
- **Evaluation:** the query is evaluated locally over every open issue of the repo, PRs excluded,
  from one paginated listing. The usual rules then apply: the skip reasons, priority order,
  `-Triage` and `-DryRun`. `-DryRun` also prints the query in canonical form and the number of
  matches.
- **Errors:** a malformed query prints the column and what was expected, and exits 2 before
  anything is read or written.
- **`-Watch`:** the query is the watched spec and is re-evaluated on each rescan. Rerun
  `-Queue 'where: <new query>' -Watch` to change the saved query or switch between a label and a
  query. `-Prune` removes pending members that no longer match the new spec; members already in
  another state stay in the queue. `-DryRun` shows the settings change and the members that would
  be pruned without writing the queue file. `-Prune` requires `-Watch` and a label or query spec.
- **Quotes and your shell:** a `.cmd` (and Windows PowerShell 5.1 calling any program) splits an
  argument at its inner double quotes. So `install.ps1` puts `github-workbench.ps1` next to
  `github-workbench.cmd` and PowerShell (pwsh and 5.1) runs it instead: it hands your arguments over
  intact, as JSON through the environment. Double-quoted labels inside a query then arrive exactly.
  From **cmd.exe or Git Bash**, `github-workbench` is the `.cmd`: quote labels inside a query with
  **single quotes** there (`-Queue "where: bug AND NOT 'needs design'"`). The same applies in
  PowerShell when its execution policy does not allow local scripts (`Restricted`, `AllSigned`);
  the installer then leaves the `.ps1` out and says so.

A label spec (`bugs`, `label:X`) or a `where:` query queues only issues nobody is handling yet. Each issue it leaves
out is printed as `#N skipped: <reason>`:
- `pr`: an open pull request will close it, or an open PR is on its `issue-<N>-*` branch in this
  repo;
- `session`: a workbench session `#N ...` is open for it in the repo's workspace or a workspace of
  one of the repo's queues;
- `checkout-lock`: a launcher currently holds its checkout;
- `checkout`: its checkout exists from an earlier loop outside this queue (resume or delete it);
- `queued (<state>)`: it is already a member of this queue.

Skipped issues are not recorded, so a later run or `-Watch` rescan looks at them again. For
example, a PR closed without merging makes the bug eligible again. A lookup that fails stops the
start and changes nothing; on a rescan, that scan adds nothing. An explicit list is never
filtered. `-DryRun` prints the members it would add, the skipped issues with their reasons, the
setting changes, and whether it would start the queue or append to it (running or stopped).

The repo's queue is appended to when one exists, running or not, and `-Watch` onto a queue started
from a list turns watching on. A switch on an append (`-Autonomous`, `-Implementer`, `-AutoMerge`,
`-BigReview`, `-Parallel`) changes the queue for every member launched from then on, including ones already
waiting. The append prints each such change (`settings: autonomous null -> true`).

A visible, restart-pinned `#queue owner/repo` conductor starts one issue at a time by default
(`-Parallel 1..8`). Each issue has its own clone, Claude, Codex, and review relay. A PR-open, blocked,
or no-PR closed report releases the initial-work slot immediately. Human-directed fixes on an earlier issue can continue
alongside later issues. Agents never merge or approve PRs. Queue launches preserve focus, and Claude
does not open revdiff automatically; run `wb.py human-review --base origin/main` in the issue's
context to open it on demand, or review on GitHub.

Every line the `#queue` conductor, the `#triage` watch and each relay print in their panes starts with
the local time: `HH:MM:SS `, or `YYYY-MM-DD HH:MM:SS ` on the first line and on the first line after the
date changes (#78). JSON output (`-DryRun`, member context) and log files are not stamped.

Rerunning appends new issues without duplicates. Saved parallelism is preserved unless explicitly
changed. A watched spec (a label or a query) is checked every five minutes; empty or temporarily failing scans keep
waiting. Without `-Watch`, the conductor exits after admission work finishes and writes a summary
snapshot beside `~/.agworkbench/queues/<owner>/<repo>.json`. Per-issue review continues; rerun the
queue to refresh its PR states. There is one watched spec per queue. `-DryRun` resolves and shows
the proposed members without starting anything. `-Retry` repairs failed/incomplete launches using
their saved checkout and conversation; ordinary reruns leave failures for the human to inspect.
Queue state corruption is reported without resetting it. Restore a moved/deleted established
checkout before retrying. The configuration file selected when the queue was created is retained,
including `AGWORKBENCH_CONFIG` overrides.

Members publish local `.workbench/state/loop.json` reports through `wb.py loop-state` instead of
mailing a conductor agent. The conductor reads those reports; each issue's mailbox and one Claude
background waiter continue to handle human review independently. Queue mode does not attach to
an existing non-queue workbench loop. Internal `-QueueMember`, `-QueueAttempt`, and `-QueueToken`
arguments are supplied by the conductor, not ordinary launch commands.
When a closed issue needs no change, `wb.py loop-state done --no-pr --reason "<why>"` records it as
`closed`, releases its queue slot, and counts it separately from `merged` in the summary.
In queue mode, `wb.py loop-state done --pr N --sha S` also reports the PR to the conductor.
The conductor can recover a PR from `loop-done.json` if that report fails. If a member's
sessions disappear, it checks the branch PR and issue after a grace period and releases the
slot when a branch PR merged or, with no open PR on the branch, the issue closed. An operator
can record a missing PR with
`python lib/conductor.py mark --file <queue.json> --number N --pr <url> --reason "<why>"`;
the action is recorded in the queue and its `operator.log`.

**Named queues (#66).** A repo can run more than one queue at once, for example a main queue for
P0/P1 with Claude and an evaluation queue for P2 with Kimi whose PRs wait for review:

```powershell
github-workbench -Queue 'where: priority IN [P2]' -Repo yeroo/docxy -QueueName kimi -Implementer kimi -NoAutoMerge -Watch
```

- **Its own everything:** the queue file is `~/.agworkbench/queues/yeroo/docxy.kimi.json`; its
  conductor is `#queue yeroo/docxy (kimi)`; its members, relays and helpers open in the agwinterm
  workspace `docxy-kimi` (or `-Workspace <name>`, fixed at the first start); its checkouts are
  `docxy-kimi-issue-N`. Implementer, revmux profile, autonomy, auto-merge and parallel are its own.
  Without `-QueueName` it is the main queue (`docxy.json`, the `docxy` workspace), as before. A
  queue name is 1-32 of `a-z 0-9 -`; `main` is reserved.
- **Exclusive claims:** an issue that is a member of one queue of the repo, in any state but merged
  or closed (a failed one included, since `-Retry` revives it), is skipped by every other queue with
  `#N skipped: claimed by queue <name>`. A closed member claims too while its close is still pending
  or stuck, or while its issue session is open in its queue's workspace, since its loop can come
  back (a reopened issue); reading the terminal for that fails the add rather than guess. That holds for explicit lists and watch rescans too. The
  check and the write happen under one repo-wide lock, so two queues adding the same issue at the same
  moment admit it once. A queue file of the repo that cannot be read stops the add rather than
  counting as "no claim". The in-hand skips also look in every queue's workspace and in the plain
  `docxy-issue-N` checkout.
- **No triage in named queues:** `-Triage` with `-QueueName` is refused. Triage stays the one
  repo-wide `github-workbench -Triage -Repo yeroo/docxy -Watch`, and named queues read the priority
  labels it sets.
- **Review profile:** `-RevmuxProfile <name>` saves a profile on the queue and passes it to every
  member the conductor launches. It holds for conductor launches only: a single-issue launch (a
  manual resume, the planner's `-Failover`) uses the tool's default, and a member the conductor
  routes to another tool because of a usage limit gets that tool's default too. The default for
  Kimi is `kimi-mixed` when the installed revmux lists it (`revmux config`), else `claude-only` with
  a warning.
- **Single-issue launches of a named member:** `github-workbench <owner/repo>#N` (with `-Failover`,
  `-Implementer`, ...) run from inside a named queue's checkout uses that checkout and its
  workspace, proven by its `.workbench/state/queue-member.json`. From elsewhere it does the same
  when `<repo>-issue-N` has no clone and exactly one `<repo>-<queue>-issue-N` has such a membership;
  two are refused, naming them. Otherwise it is the plain `<repo>-issue-N` launch. Relaunching a
  named member through its conductor is `-Queue <spec> -QueueName <name> -Retry`.
- **Names:** `<repo>-<queue>` should not be another repo's name. A clone that the queue has not yet
  established is reused only when its origin is the queue's repo.
- `-Cleanup` recognises `<repo>-<queue>-issue-N` checkouts and checks their sessions in the workspace
  their queue membership records.

Claude's conversation ID, original project directory and pane binding live in
`.workbench/state/claude.json`. The launcher reserves the ID before starting Claude, so an
interrupted first launch can retry with the same ID. The pane script resumes that exact
conversation when its transcript exists, or starts the reserved conversation otherwise. A
replacement Claude pane gets a new reservation and the old record is archived. When adopting a
running Claude, its `CLAUDE_CODE_SESSION_ID` and transcript must be available; a missing identity
is refused before adoption. `claudeArgs` cannot override conversation identity or launch mode.
For older workbenches without a saved identity, the launcher recovers the newest unambiguous CLI
transcript for the checkout. If it cannot identify a running Claude conversation, it leaves that
pane unpinned and prints a manual repair command. A per-checkout lock prevents overlapping launches
from reserving different identities for the same pane.

Codex's restart command uses `-Resume` to select the newest interactive rollout for the checkout
by its metadata timestamp, ignoring review/exec rollouts and other directories. It reapplies the
same sandbox, approval and environment policy. If no matching rollout exists, it starts fresh.
Resumed Codex continues interrupted implementation or fixes; otherwise it checks mail and waits.
Resumed Claude starts its background mail waiter and continues its interrupted phase.
Both pane scripts support `-WhatIfOnly` to inspect the command without starting an agent or
changing the workbench state. Claude transcripts use `CLAUDE_CONFIG_DIR` when set (otherwise
`~/.claude`); Codex rollouts use `CODEX_HOME` when set (otherwise `~/.codex`).

```
 github-workbench owner/repo#42
   ├─ full clone ~/source/workbench/repo-issue-42, branch issue-42-<slug>
   ├─ session "#42 <slug>":   [ Claude  |  Codex ]
   └─ session "#42 relay":    rings panes on mail; watches the PR

 Claude  /start-github-issue owner/repo#42
   1 intake      reads the issue, writes .workbench/issue.md for Codex
   2 plan        drafts plan.md  ⇄  Codex critiques  … until "AGREED: plan vK"
   3 implement   Codex, on the issue branch; commits; runs the tests
   4 review      revmux round in its own session → Claude verifies findings
                 ⇄ Codex fixes or disputes with evidence … until clean
   5 PR          Claude pushes and opens it (Codex has no network)
   6 you         revdiff opens in front of you — annotate, press q — or review on GitHub
                 → each round of feedback becomes a fix round for Codex
   7 merged      you merge; the relay sees it; both agents stop
```

The relay types a one-line `Chat from Workbench:` pointer into the recipient's pane when mail
arrives. Claude also keeps one `wb.py wait-mail` command running through its background execution;
the command checks its unread inbox immediately and then waits, waking Claude on mail or timeout.
Reports from revmux and your revdiff annotations arrive through the same mailbox. You never need
to type anything to keep the loop moving: a draft in Claude's composer only delays the relay's
ring, which normally raises an alert after a minute (ambiguous Claude text has a longer delay,
described below); the background waiter still wakes Claude. Codex uses
the relay's queued pointers. Claude stops its waiter when the loop is complete.

### Issue triage: priority labels from the private spec repos

`-Triage` gives every open issue of a product repo exactly one `priority:P0`..`priority:P3` label,
plus `ux` when UI/UX is the reason. It judges each issue against the product's private spec
repos. The labels must already exist on the repo.

```powershell
github-workbench -Triage -Repo yeroo/docxy              # every open issue without a priority: label
github-workbench -Triage -Repo yeroo/docxy -Watch       # keep doing it for new ones, every 5 minutes
github-workbench -Retriage -Repo yeroo/docxy -DryRun    # re-judge labelled ones too; print, write nothing
github-workbench -Triage -Retriage -FollowUps -Repo yeroo/docxy -Limit 200
github-workbench -Queue bugs -Repo yeroo/docxy -Autonomous -Triage
```

Configure it locally in `~/.agworkbench.json`, never in the repo:

```json
"triage": {"yeroo/docxy": {"specRepos": ["yeroo/docxy-project-spec", "yeroo/docxy-word-spec",
                                          "yeroo/docxy-excel-spec"], "model": "sonnet"}}
```

**The rules:**
- **P0:** it blocks an open spec issue, or it has a severe user-facing impact (a crash, data loss,
  an unusable or silently wrong flow).
- **P1:** a visible UI/UX defect against the reference app or `docs/ui/`, or it blocks a spec
  enabler or the harness.
- **P2:** a correctness defect in a spec area that blocks nothing open.
- **P3:** out of the current spec scope, cosmetic, or internal only.

**How it decides (`lib/triage.py`):**
1. **The facts, without a model.** triage.py reads every configured spec repo that exists: its
   open issues and a shallow cached clone (`~/.agworkbench/spec-cache`). A repo that does not exist
   is skipped with a note, so `docxy-excel-spec` joins once it exists. GitHub answers a private
   repo your gh account cannot see exactly like a missing one, so if **none** of the configured
   spec repos can be read, the run stops (check `gh auth status`). Any other failure (network,
   auth, rate limit, clone, a stalled fetch) stops the run before anything is written. Judging
   without the specs would put everything too low. The cache is only ever touched through its own
   `.git` (never git's discovery of a parent repo), and a per-repo lock keeps a `-Watch` session
   and the queue's triage runs from re-syncing a clone while the model reads it.
2. **References.** Only an exact reference in an open spec issue's title or body counts:
   `docxy#12`, `yeroo/docxy#12`, or the issue's URL. `docxy#120`, `docxy-word#12` and a bare `#12`
   do not, and spec comments are not scanned. A referenced **bug**, or an issue referenced by a
   spec bug mirror (title `bug:` or a `bug` label), is **P0 without asking the model**. Any other
   referenced issue gets a P1 floor. Minor follow-ups on the deterministic P0 path still ask the
   model whether an exception applies.
3. **The model,** everywhere else. It runs as `claude -p` with the text of
   `claude/commands/triage-issue.md` (also installed as `/triage-issue`). It runs `--restricted`,
   with only Read, Grep and Glob, no MCP servers, no settings but the quiet file, and a JSON schema
   for its answer, within 300 s. triage.py enforces the rules, whatever the answer says:
   - the floor is never lowered except when the follow-up cap applies;
   - `follow-up` and `follow-up-nested` issues with trusted `Severity: minor` (or no trusted
     major severity) are capped at P2 unless they involve save/load data loss or corruption, a
     crash or hang, a document failing to open or save, or a security problem. Trusted major and
     blocker follow-ups keep normal triage. The public comment states the cap; the private log
     retains the uncapped grade;
   - a P0 from the model alone, for an author outside the repo (not OWNER, MEMBER or
     COLLABORATOR), is written as P1. Anyone can file a public issue, and its text is untrusted
     input to the model, so it must not be able to put itself at the front of the queue;
   - an answer that breaks the schema, or cites a spec issue that is not in the facts, fails that
     issue with nothing written; the others go on;
   - a usage-limit or auth failure stops the run.

**Nothing private reaches the public repo.** The rationale is written to the private log first;
then the public issue gets its labels and one comment from a fixed template, for example
`Triaged priority:P1 (user-facing UI/UX).`, carrying the planner marker. If that comment fails
after the label is on, the run says so (`labelled priority:P1, but the public comment failed`);
the issue counts as triaged. Capability ids, spec text, spec titles and links never appear there. The rationale goes to
a "Triage log" issue in the first spec repo that exists: one comment per decision, with the spec
refs and any rule that changed the model's answer. The issue asked for a comment on the blocking
spec issue; one log is less noise there.

**What gets triaged.** Open issues without a `priority:` label, oldest first, at most `-Limit`
(default 20) per run. A label already there counts as triaged, including one you applied by hand.
The labels are read again just before writing, so a label you add meanwhile wins. `-Retriage`
also re-judges the labelled ones and replaces their label.
`-FollowUps` limits a manual triage or retriage run to the two follow-up labels, useful for
applying the new cap to existing issues without repeatedly taking the oldest 20 open issues.
`-Watch` opens a visible
`#triage owner/repo` session that re-scans every 5 minutes; nothing that goes wrong in one scan
ends it. An issue that keeps failing there is retried with a growing delay, then left alone after
3 failures, with one notification. Only the watch counts failures: a manual or queue run always
tries again.

**The `kimi` label** (#77), for a product whose entry has `"kimiLabel": true` (the label must exist on
the repo): the model also judges whether the issue suits Kimi Code, by named rules from the owner's
evaluation (#82), and answers with the one that decided it (`kimiRule`). Excluded, checked first:
`p0-p1`, `save-path` (save, serialise or data-loss paths), `outside-format` (outside file-format or
interop work without an in-repo oracle), `umbrella-batch` (umbrellas, batches, and a leftovers list
that spans more than one area), `new-subsystem` (new subsystems, large features such as a whole new
editor), `multi-crate`, `no-oracle`, and `not-narrow` (no allowed rule fits). Allowed, for a P2 or
P3 issue no exclusion holds for: `narrow-fix` (one crate or a small area, checkable against tests,
fixtures, an oracle file or sibling code), `leftovers-one-area` (a `Leftovers from #N` list whose items
all sit in one crate or area, none on a save path), `harness-two-crates` (uiharness verbs or one
app's control surface on at most 2 crates, the second only the verb's thin host side) and
`ui-single-view` (a small UI/UX fix in one view). triage.py enforces the P0/P1 rule and the
deterministic P0 path itself (`spec-reference`; `not-p2-p3` when an issue lost its P2/P3 label before
the write: two ids only the tool gives), and an answer whose rule contradicts its verdict is not suitable.
Suitable adds `kimi`, unsuitable removes it, and the reason goes to the private log only
(`kimi: yes (leftovers-one-area) - ...`). A `kimi` label you set or removed by hand wins: triage
keeps the ids of the label events it caused, so any other `kimi` event on the issue makes it leave
the label alone (`kimi: human override`). `-Triage -Retriage -KimiOnly -Repo owner/repo` re-judges
only the label, on open P2/P3 issues, with no priority change and no public comment, and ends with
`kimi: N labelled, M cleared, K kept (human)` (`, U unchanged` and `, F failed` when there are any;
a dry run says `kimi (dry run): N would be yes, M would be no`).

**The queue.** Pending members are admitted P0, then P1, then untriaged, then P2, then P3, oldest
issue first within each. The conductor reads the labels for the whole repo on each refresh, and
active or PR-open members are never touched. With `-Queue ... -Triage`, an untriaged pending member
is triaged in the background, one at a time, before it may be admitted. A failed triage admits it
at the untriaged rank. A queue that is already running picks this up only after a restart: close
its `#queue` session and start it again with `-Triage`. Its pending members are then triaged and
sorted, and its active ones are left alone.

## Safety model

**Codex** runs `--sandbox workspace-write --ask-for-approval never`, rooted at the issue's own
clone. Network access and writes outside the clone are blocked, and the two settings that tune the
sandbox are pinned on the command line so a config file cannot loosen them. `"allowNetwork": true`
in `~/.agworkbench.json` is the one way to open the network. Nothing in `codexArgs` may re-decide
the policy — not by flag, config key, profile, or alias; the test suite pins every spelling.

Because its sandbox denies the terminal's control pipe, Codex cannot type into any pane — and
doesn't need to: it writes mail files inside its clone, and the relay rings Claude.

**Forks (#71).** In a clone of a fork, `gh` without `--repo` resolves to the fork's *parent*: a
leftovers issue once landed on the upstream repo that way. Every `gh` call the workbench makes names
the checkout's own repo, read from its `origin` remote (never `gh repo view`), and `wb.py` refuses,
before `gh` runs, any write aimed at another repo (`refused: gh issue create targets up/r, not this
workbench's repo fork/r`). merge-check fails a PR of another repo with a `repo:` line. The launcher
also runs `gh repo set-default <owner/repo>` in every checkout, and checks it took, so the agents'
own `gh` commands default to the right repo too.

### Claude as the implementer

With `"implementer": "claude"` in `~/.agworkbench.json`, or `github-workbench <issue> -Implementer
claude` (for example when Codex is out of quota), the right pane runs Claude Code on
`/workbench-implementer` instead of Codex. The loop does not change: the implementer's mailbox box
is still `codex`, the relay rings it with Claude's profile (Return submits, the mid-turn hold, the
ambiguous-composer rules), and it gets its own conversation record
(`.workbench/state/implementer-claude.json`) and restart pin, like the planner's. The planner never
commits the implementer's uncommitted work, because both panes share one index; the implementer
commits its own. A queue started with `-Implementer` passes the switch to every member.

**A checkout keeps its implementer.** The choice is saved in `.workbench/state/implementer.json`,
and a later run without `-Implementer` reuses it, so a repair run never swaps the agent under a
running loop. A run that asks for the other tool is refused (exit 2, nothing changed) while the
right pane holds a running agent. Close that agent, or leave it at a shell prompt, and rerun.

**It is not sandboxed the way Codex is.** On Windows, Claude Code has no OS sandbox for its shell.
Also, unlike Codex, it inherits your network and your authenticated `gh`. The launcher always
passes `--disallowedTools` for `git push` and `gh` through both of Claude Code's shell tools
(`Bash(git push:*)`, `Bash(gh:*)`, `PowerShell(git push:*)`, `PowerShell(gh:*)`), and, unless
`allowNetwork` is set, `WebFetch` and `WebSearch`. These deny rules hold under `--dangerously-skip-permissions` too, but they are a
guardrail, not a boundary: a command can be spelled around a prefix rule. `claudeArgs` applies to
both Claude panes. For the implementer, flags that would widen its tool policy are refused:
`--add-dir`, `--permission-mode`, `--allowedTools`, `--disallowedTools` and `--settings`. Without
`--dangerously-skip-permissions`, it asks before commands, and the loop waits for you.

**Review rounds follow the implementer.** `wb.py revmux` uses the revmux profile saved for the
checkout: `comprehensive` with Codex, `claude-only` with Claude, and `kimi-mixed` with Kimi when the
installed revmux has it (else `claude-only`, #66), so a round never depends on Codex's quota.
`revmuxProfile` in the config overrides them, a queue's `-RevmuxProfile` overrides that for its
members, and `--profile` overrides it for one round.

### Kimi Code as the implementer

With `"implementer": "kimi"`, or `github-workbench <issue> -Implementer kimi`, the right pane runs
[Kimi Code](https://moonshotai.github.io/kimi-code/) (`kimi`, tested with 2.1.1). The loop is the
same: the mailbox box stays `codex`, the relay rings it with Kimi's profile (Return submits, typed in
its own call, well after Kimi's 120 ms paste-burst window; mail is held while a turn's spinner is
above its composer), and failover and the queue know it. `lib/pane-implementer-kimi.ps1` starts it,
and `lib/kimi.py` prepares what the pane needs.

- **Where kimi comes from.** `kimiPath` in the config, else `kimi` on `PATH`, else
  `%USERPROFILE%\.kimi-code\bin\kimi.exe`. The launcher refuses a launch, and a failover to Kimi,
  before anything is recorded or stopped, when kimi is missing, `kimiArgs` would re-decide its
  policy, `kimi doctor` fails, the web-tool check below fails, Git Bash is missing, or the clone
  tracks (or someone else wrote) `.kimi-code/AGENTS.md`. The pane checks these again. `-DryRun`
  makes the same checks except `kimi doctor`, which would run kimi. In a queue, such a refusal
  counts as infrastructure: the member is deferred with back-off, not failed.
- **`--yolo` ("Ask When Needed"), never `--auto`.** Routine edits and commands run on their own.
  Kimi still stops for commands it rates dangerous (a recursive delete, say), for sensitive files and
  for `.git` control paths. Our guards cover only push, gh and the web, so `--auto` would remove the
  only stop left. A pane on Kimi's approval prompt is a dialog: the relay holds its mail and the
  stall watch reports it. Answer the prompt yourself.
- **The role is `<checkout>\.kimi-code\AGENTS.md`**, not an agent file. Kimi Code 2.1.1 ignores
  `--agent-file` and `--agent` in its interactive mode: only `kimi -p` binds the profile. With either
  flag, the session's wire log still shows `profileName: "agent"`, no disallowed tools, and none of
  the file's prompt (verified on 2026-09-29; see #65). Kimi loads `.kimi-code/AGENTS.md` next to the
  repository's own `AGENTS.md`. The pane writes it at every launch from `kimi/AGENTS.md` and adds it
  to `.git/info/exclude`. It refuses to start if the repository tracks that file or someone else
  wrote it.
- **No `git push`, no `gh`.** Kimi's shell tool is Git Bash. Its launcher (`Git\bin\bash.exe`) puts
  `/mingw64/bin` first on `PATH`, so a shim directory on the inherited `PATH` would be shadowed. The
  pane pins `KIMI_SHELL_PATH` to that bash and sets `BASH_ENV` to a script that puts
  `.workbench\state\kimi-bin` (a `git` that refuses `push` and `send-pack`, and a `gh` that refuses
  everything) back in front. For the spellings a shim cannot catch (`git.exe push`, an absolute
  path, `cmd /c git push`), `GIT_CONFIG_*` rewrites every GitHub push URL to a scheme git cannot
  speak, and `GH_TOKEN` is set to a token GitHub refuses. All of this lives only in Kimi's process
  tree. It is a guardrail, not a boundary: Kimi could unset it, and its role says not to.
- **The web tools are your `[tools] disabled`.** No per-session switch exists in Kimi 2.1.1, and the
  launcher never edits your global config. Unless `allowNetwork` is set, it refuses to start Kimi
  until `config.toml` (under `KIMI_CODE_HOME`, else `~/.kimi-code`) has:

  ```toml
  [tools]
  disabled = ["FetchURL", "WebSearch"]
  ```

  That turns them off for every Kimi session on the machine, and that is why the launcher asks you
  rather than doing it. Kimi's question, plan-mode and subagent tools (`AskUserQuestion`,
  `EnterPlanMode`, `Agent`) cannot be turned off per session either. The role forbids them, and a
  pane parked on a question shows up as a dialog.
- **Trust, console and restarts.** Kimi's "Trust this folder?" dialog exits on Esc, so the pane
  writes Kimi's own trust record for the clone (`workspace-trust/wd_<name>_<hash>`) when it is
  missing. It runs Kimi with a UTF-8 console, without which every glyph the relay reads arrives as
  mojibake. `KIMI_CODE_NO_AUTO_UPDATE=1` keeps it one process. The restart pin runs the pane with
  `-Resume`: `kimi -c` continues its last session for the clone, and a note from `relay` in box
  `codex` makes the relay ring it, since the relay rings each mail only once.
- **Limits.** Kimi retries a 429 itself (`Retrying (n/10)`). The relay counts a Kimi limit only as
  its session error (`Error: [provider.<code>] ...` followed by its `/export-debug-zip` hint), as the
  last thing above an idle composer, and only when the message names a quota or usage limit. A bare
  rate limit after the retries is not a reason to fail over. No real Kimi limit frame has been seen
  yet, so those fixtures are synthesised (`tests/fixtures/limits/README.md`). If a limit is missed,
  the stall watch still reports the idle pane.

**The relay** types only into the two panes of its own session, only into an agent's composer it
can prove is empty, never into a dialog, and never answers a prompt. A refusal before typing waits
for the next tick. After typing a pointer once, it verifies submission from an empty composer
and records whether the pointer was submitted or queued. Before typing or submitting, it rereads
the composer after any cursor query and requires both parsed snapshots to agree. A stuck pointer gets up to three more
submit-key presses, each guarded by a fresh composer check. A clipped pointer must still show its
complete message ID. Codex gets one Return fallback only
when no running-turn or queued-input evidence is visible. It never submits a changed draft or
a dialog. This verifies submission, not that the agent has read the mail. Failed submissions
alert immediately; mail held for a minute also raises a blocked status, sound, blink, and desktop
notification naming the recipient, message, and reason. Alerts repeat at most every five minutes
per message. Failed rings stay unannounced and can be sent again once the composer is empty.
A pointer that still sits typed-but-unsent after that (#96) is looked for on every
`--limit-interval` tick, watching or draining: an idle pane whose composer holds exactly the
relay's pointer for mail still unread, the same on two looks, gets one more submit key per look,
never the text. Three attempts that do not submit log `UNSUBMITTED` and alert. Stall pointers and
the usage-limit probe carry no message ID and are not rescued.
For Claude, unrecognized one-row text with its caret at the starting column is ambiguous: possibly
a suggestion, or a draft with its caret at the start. The relay still refuses to type, but this
pre-write hold alerts only after the same full text persists for ten minutes. A change in that text
restarts the delay; changing from ambiguous text to an ordinary draft starts the one-minute delay,
subject to the same five-minute notification throttle. Existing alerts keep their status-reset
bookkeeping. Ambiguous text after a submit does not prove success and still results in a failed
submission alert. This is a diagnostic and alert mitigation, not the delivery fix requested in
[#16](https://github.com/yeroo/agworkbench/issues/16), which remains open. Safe delivery through a
suggestion requires the distinguishing styled read tracked in
[agwinterm#319](https://github.com/yeroo/agwinterm/issues/319).
When alerted mail is delivered or independently read, the relay clears its last outstanding alert
for that recipient to idle. That reset can race a newer agent-hook status; avoiding the race
would require terminal support for conditional status ownership.
After merge or closure, the relay keeps delivering the final notices and alerting on holds for
up to 30 minutes, then logs any notices still waiting before exiting. A failed status reset is
persisted and retried on later ticks, including after a restart. Relay dry runs neither file
GitHub-event mail nor save announcement state.

The relay follows the newest open PR from the watched repository and branch. It also catches a
PR created and finished between polls: discovery and discussion mail are followed by the normal
merge or closure notices and drain. To distinguish these from older finished PRs, it saves a
branch-specific `watch_since` from GitHub's server Date before polling. Creation at or after that
second qualifies; an older PR first seen finished is recorded and ignored. Missing creation
timestamps are retried. If the server Date is unavailable, PR watching waits and retries while
mail delivery continues; there is no local-clock fallback or coverage before initialization.
The boundary survives same-branch restarts, resets on a branch change, and is initialized the
same way when upgrading state that has no boundary.

OPEN observation also survives restarts: if that PR finishes while the relay is offline, its
merge or closure still gets final notices and a drain. Once those notices and status resets are
resolved, the PR is retired. A MERGED PR ends the relay run and starts the autonomous close. A
CLOSED unmerged PR leaves the same relay watching for another PR or a later no-PR completion. If
several eligible PRs finished between polls, the newest by creation time (then PR number) is
handled first; the remaining PRs can be found on subsequent polls after a CLOSED PR, or on restart
after a MERGED PR. Pending final notices from older relay versions are also drained once for
compatibility.

When a newer open PR appears, the relay checks the previous PR before switching. If the watched
PR has finished, the relay drains its terminal notices. After a MERGED PR it exits and the newer
PR waits for the next run. After a CLOSED unmerged PR it keeps running and can watch the newer PR.
Otherwise, it logs that the unresolved watch was superseded. PR events are saved in an outbox
with IDs derived from GitHub event identities before publication. A restart
replays that outbox without overwriting existing mail or resurrecting mail already read or
archived, then resumes normal delivery and final-notice draining. Publication errors are logged
and retried on later ticks. A branch change discards any unpublished outbox from the old branch.
Opening notices use creation time or the latest GitHub `reopened` timeline timestamp, so a
reopening gets its own notice even after relay-state loss. Decision notices include the previous
decision and the PR update time to distinguish repeated changes. Windows publishes complete mail
atomically using a hard link or a no-replace rename; replay repairs an incomplete header while
holding the message ID's publication lock.

**Nobody merges but you, unless you turn on auto-merge.** Claude may push the branch and open the
PR. It never approves its own PR and never force-pushes over commits you have reviewed. It never
merges, unless you opted in for that checkout.

### Full autonomy (opt-in)

`github-workbench <issue> -Autonomous`, `-Queue <spec> -Autonomous`, or `"autonomous": true` in
`~/.agworkbench.json` lets a loop finish without you:
- **It merges** behind the auto-merge gate. Autonomy implies auto-merge, and `-NoAutoMerge` on an
  autonomous checkout is refused.
- **It files follow-up issues.** Every deferred finding, and every out-of-scope item in the agreed
  plan, is recorded before the merge (`wb.py follow-up`). Minor, Immaterial and plan items from a PR
  share one `Leftovers from #N: <issue title>` checklist issue. Major and blocker items get separate
  issues; the planner uses `follow-up add --own-issue` when a planned out-of-scope feature needs its
  own issue. The label is `follow-up`, or
  `follow-up-nested` for a follow-up's own follow-ups, so a `-Watch label:follow-up` queue chains at
  most one level. merge-check refuses while any is unfiled.
- **A problem reported again gets more urgent (#42).** Before filing, `follow-up file` looks for an
  issue that already describes the finding: the same normalised title among the follow-up issues
  (open, or closed as completed, which it reopens), or an unchecked checklist line in an open,
  trusted leftovers issue. It then makes one restricted `claude -p` call over the
  open follow-up and bug issues, where only a high-confidence answer naming an issue opened by the
  owner, a member or a collaborator counts (anything else is linked as possibly related). On a match it comments
  a duplicate report there instead of filing. At 2, 3 and 5 reports in all, the issue's label goes
  up to `priority:P2`, `P1` and `P0`, never down; an untriaged issue gets no label below P1
  (untriaged is already admitted before P2). A new issue gets its priority from triage when the
  repo has a `triage` entry, and from the finding's severity otherwise. Only comments by the repo's
  owner, members and collaborators that carry the planner marker count as reports.
- **Only Minor findings may be deferred.** A Major or blocker review finding stops the merge and
  waits for you, whether it was deferred or ended disputed. Minor and Immaterial ones may be
  deferred as follow-ups. The merge comment lists the leftovers issue with its items and every
  separate issue with its severity and link.
- **It closes the sessions.** After a MERGED PR, or after a closed issue with a recorded no-PR completion,
  the relay first closes the
  issue's revmux and review sessions that have finished, each on its own evidence (below), whatever
  the agents are doing; a running revdiff stays open and is named. The issue session and the relay
  itself close only once the planner has recorded `wb.py loop-state done`, the planner has read
  all its mail, and both panes are unchanged for 30 s with empty composers and no
  `.git/index.lock`. A relay pointer left in a settled, idle composer for mail that no longer needs
  reading (read, or implementer mail the close ignores) is cleared with Ctrl+U first, and one for
  mail still unread is submitted (#96); anything else in a composer still holds the close. The implementer's unread mail works differently (#44). The relay's own
  `PR #N MERGED` notice and anything sent after the merge or no-PR done record, such as the planner's "loop complete"
  note, never hold the close. Older unread implementer mail holds it for 10 minutes; then the
  close goes ahead and logs the ids. It only ever touches the checkout's workspace (a named
  queue's own, else the repo's). Every step
  goes to `.workbench/state/relay-close.log`. Nothing else is overridden on a timeout: it alerts
  you instead.
- **It deletes the checkout** (#41, config `cleanup`, default `merged`). Once the issue session is
  closed, a detached `lib/cleanup.py after-close` waits (up to 10 minutes) until no `#N` session is
  left in the checkout's workspace (a named queue's own, else the repo's), then deletes the clone, but only when it is safe: nothing
  uncommitted, untracked or stashed, no linked worktree, no submodule, no `.git/index.lock`, no launcher or queue
  still using it, and every local commit on a remote-tracking ref or inside the merged PR's head.
  For a no-PR completion, only remote-tracking refs can vouch for local commits.
  Otherwise the checkout stays and the reason is logged. The delete renames the directory to
  `<name>-issue-<N>.deleting-<ts>` first, which Windows refuses while anything holds a file or its
  cwd inside it. Every decision goes to `<checkoutRoot>/.agworkbench-cleanup.log`; a refusal also
  goes to the checkout's `relay-close.log`. `"cleanup": "build"` deletes only `target/`,
  `node_modules/`, `bin/` and `obj/` directories holding no tracked file; `"off"` keeps everything.

How the close proves each thing (#33):
- **Agents:** every Claude the workbench launches (planner and implementer) starts with prompt
  suggestions off (`CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION=false`, plus `--settings` pointing at
  `.workbench/state/claude-settings.json`). Otherwise the greyed suggestion in an idle composer
  makes it impossible to prove the composer empty: the relay would neither ring the agent nor
  close its session. `claudeArgs` may not pass its own `--settings`; put your settings in
  `~/.claude/settings.json`. An adopted Claude (your own session) needs
  `"promptSuggestionEnabled": false` there too, and the relay's hold and close logs say so.
- **Helpers:** `wb.py revmux` and `wb.py human-review` start their sessions in agwinterm's direct
  command mode: the helper runs with no shell around it, and when it ends its pane stays on screen
  with its input closed, so nothing can be typed there and nothing more is printed. As its last act
  the helper writes a completion marker (`.workbench/state/helpers/<pane>.done`, holding what the
  pane showed). A helper closes when its marker exists, no shell is live in its pane, the pane shows
  exactly the marker's rows, and it has been unchanged for 30 s. Helpers close first, even while the
  agents are still busy. A helper session opened the old way, inside a shell, is left for you.
- **Finished helpers, before the merge (#84):** on every loop, autonomous or not, the relay also
  closes a `#N revmux r<K>` or `#N suite <label>` session as soon as its result has been read: its
  completion marker exists and the result mail it names (the revmux report, or the suite's result)
  has been read in the box it went to. The pane is not checked - once the result is read the
  session goes, whatever it shows. A helper whose result is still unread stays, and so does your
  revdiff (`#N your review`). The report and log files in `.workbench/review/` stay; each close is
  logged in `.workbench/state/relay-close.log` and in the relay's pane. `closeHelpers: false` keeps
  every helper open until the loop's close, as before.
- **A relay that died or gave up:** the queue's conductor runs the same close for a merged or no-PR closed member
  whose close has been pending for 15 minutes and whose `#N relay` session is gone. While that
  relay is still alive, the member is only flagged `closeStuck` in the queue file, because one
  closer at a time. A queue member's relay whose close gives up after its 10-minute wait hands it
  over (#44), but only while the queue's conductor is running. It keeps `close_pending` in
  `relay.json`, records `close_handoff` and closes its own session. The conductor stays up for that
  close and retries it once, with a fresh wait. Without a running conductor (a queue that is not
  watching finishes once its last member has a PR), the relay alerts you and leaves its session
  open, as before. A close that cannot complete is refused and flagged, never forced.
- **Members launched before this fix** still have suggestions on, so their relays keep holding the
  doorbell. Once such a loop has recorded `wb.py loop-state done`, close its sessions by hand.

It does not decide plan disagreements for you, delete an unmerged checkout, or queue its own follow-ups. To
stop it, use `-NoAutonomous` on the checkout (read again at close time) or on the queue, a hold
comment on the PR, or mail through revdiff.

### Cleaning up checkouts

Every issue gets a full clone under `checkoutRoot`, and a Rust or Node build can make one many GB.
An autonomous loop deletes its own checkout after the merge (above). For every other checkout:

```
github-workbench -Cleanup -DryRun                 # candidate <path> <size> / keep <path>: <why> / skip <path>: <why>
github-workbench -Cleanup                         # delete the candidates that pass the checks
github-workbench -Cleanup -Repo yeroo/docxy -BuildOnly   # only their build outputs
```

A checkout is a candidate when its branch has no open PR, and either its issue is closed or the
branch has a merged or closed PR. Each candidate gets the same checks as the autonomous delete, and the sweep refuses them
all when the session tree cannot be read (run it inside agwinterm). Leftover `*.deleting-*`
directories from an interrupted delete are removed; one that is a link is kept, never followed. It exits 0, 1 when it kept a candidate for a
safety reason, and 2 on a usage or GitHub error.

**The queue's disk guard.** Before admitting a member, the conductor checks the free space on the
drive of `checkoutRoot` against `minFreeGB` (default 20, in GiB as Explorer shows them; 0 turns it
off). Below it the queue admits nothing, no member changes state, the queue file gets
`diskPaused: "low disk: ..."`, and its session shows blocked with a notification. The next tick
checks again and resumes when there is space. The memory guard works the same way: below
`minFreeRamGB` (default 3 GiB) of free memory it records `ramPaused: "low memory: ..."`, and it
resumes by itself.

**Blocked members and the live ceiling.** A member blocked on a question for you frees its slot,
so the next issue starts. A member blocked by its environment keeps its slot while its issue
session is open: a usage limit it could not fail over, low disk or low memory. The planner reports
that with `wb.py loop-state blocked --environmental`, and a limit its relay announced counts too.
A member that resumes takes a slot again. It gives the slot back once its issue session has been
gone for two minutes, and takes it again if the session comes back. Whatever the slots say, the queue admits nothing while
`parallel + 2` members have live sessions: launching, active, blocked, and open-PR or
close-pending members whose issue session is still in the terminal. A
session counts as gone only after two minutes missing, and an unreadable terminal counts every
member as live. So with `-Parallel 1`,
three PRs waiting for your merge stop new launches until you merge one or close its sessions.

**Queue launch failures.** A transient launcher-start failure, launcher exit without a result,
launcher timeout, terminal, pane, agent-start or relay failure returns the member to
`pending` and retries that same member after 1, 2, 4, 8, then 15 minutes (15 minutes thereafter).
After two consecutive infrastructure failures, the queue announces `launches failing: ...` and
shows blocked; it keeps probing one member at a time until a launch succeeds. Checkout or issue
errors still fail the member and require `-Retry`. Each failed launch closes the issue and relay
sessions it created. A record in the checkout's `.workbench/state/queue-launch.json` lets a
restarted conductor or retry finish closing sessions left by an interrupted launcher. Queue mode
starts Claude only after the implementer pane has a proven shell and has been started. If the
agwinterm window is minimized or a pane is narrower than 40 columns, the launcher restores the
window without activating it and waits for a usable pane before starting agents.
If an orphaned launcher still holds its member or checkout lock after 30 minutes, that member
fails instead; stop the stuck launcher and use `-Retry`.

### Usage limits: automatic failover

When an agent hits its usage limit, **the loop now fails over by default**. The relay reads both
panes every 30 s. It recognises an agent's own limit message by its position, as the last thing
above an idle composer or right above the shell prompt after the agent exited. Text an agent
merely printed does not count: a diff, a grep, test output or a file dump. When the relay sees it
twice in a row, it tells the planner by mail and notifies you. When the limited agent is the
implementer, the planner runs `github-workbench <issue> -Failover`, which:
- takes an exited agent as it is. When the agent is still running, it stops it only when the pane
  has not changed for 90 s, there is no `.git/index.lock`, and exactly one agent process belongs to
  this checkout. It stops that process tree and never types into the agent;
- records the limit in the checkout's settings, clears the pane, and starts another tool there
  through the `-Implementer` switch: the first tool in `failoverOrder` (default claude, codex,
  kimi) that is not the limited one, has no recorded limit and, for Kimi, passes its launch checks.
  The default keeps Codex and Claude switching to each other, as before;
- lets the planner hand the work over by mail (`wb.py handover` computes the open request).

A tool with a recorded limit is never switched back to automatically. Once its limit has reset,
clear it with `github-workbench <issue> -Implementer <tool>`. Codex's "Approaching rate limits"
chooser (under 10% of its limit left) fails over the same way. Nobody answers it, and it counts
only at the bottom of the pane, where the chooser replaces the composer. The planner's own limit
cannot be failed over: you get the notification, and the loop waits. Set `"failover": false` to
have limits only reported. In a queue, a limit that any live member recorded sends later members
to the first free tool in `failoverOrder`, usually Claude (with `failover` on). If Claude is limited, or failover is off, the queue pauses with
`toolsPaused: "tool limits: ..."`. Once the limit has reset, clear it for the queue with
`-Queue <spec> -ClearLimit <tool>`. That changes nothing else: the queue's `-Implementer` stays as
it is, and setting `-Implementer` never clears a limit. Records made before the clear are ignored
from then on, and `-DryRun` shows the record it would clear. The limit strings come from the installed binaries
(`tests/fixtures/limits/`, with the command that extracted them).

### A slow queue that waits out usage limits

Kimi Code's plan has a rolling 5-hour usage limit: in the owner's evaluation it did 20-50 minutes of
work per window, then answered `403 You've reached your 5-hour usage limit`, with no reset time. A
queue that should run Kimi at its own pace, about five issues a day with nobody watching, neither
fails over nor stops for you. `-WaitOnLimit` makes it wait:

```powershell
github-workbench -Queue 'where: kimi AND priority IN [P2, P3]' -Repo yeroo/docxy -QueueName kimi -Workspace docxy-kimi -Implementer kimi -WaitOnLimit -Autonomous -Watch
```

What waits (#77), for a member launched with `-WaitOnLimit` (saved as `onLimit: "wait"` in its
`state/implementer.json`, like `-BigReview`; `-NoWaitOnLimit` goes back to failover):
- **An agent at its limit** (the implementer or the planner) keeps the member's slot. The relay
  mails the planner one `usage limit: ... waiting` note that asks for nothing, sets the pane idle,
  and holds the agent's mail without ever alerting. Every `limitRetryMinutes` (default 30) it types
  one pointer into the idle agent: "the usage limit may have reset; continue where you left off".
  An idle pane does not change when the window resets, so asking is the only way to find out. A
  limited agent answers with its limit again at no cost, and the wait goes on; otherwise it
  continues, the limit row leaves the pane, and the held mail is rung.
  Kimi's message gives no reset time, so the relay also notes when it first saw Kimi busy in the
  current window. The first probe after that comes 5 hours (plus a minute) after that time, when this
  is sooner than the next regular one (#88).
- **A limit the relay could not place** still waits (#88). Kimi can draw its limit error under a tool
  call, where it looks like that tool's output (docxy #775). Kimi's todo list, docked between the
  transcript and the composer, is set aside before the limit check reads the rows above the box
  (docxy #820). When the stall watch then finds the
  member idle with a Kimi limit error at the bottom of the implementer's pane, it starts the wait itself
  instead of pinging the planner. A planner that sees a limit the relay missed runs `wb.py wait-limit
  --reason "..."`, which starts the same wait. In a `-WaitOnLimit` loop `wb.py loop-state blocked`
  refuses any reason that mentions a limit unless the planner adds `--needs-human` (a GitHub, CI or disk
  limit is a real block): an agent's usage limit never waits for you.
- **The queue** starts no new member while any member waits. The conductor prints
  `(kimi) waiting: #N kimi usage limit since HH:MM, next try HH:MM` once and
  `queue resumed: usage limit cleared` when it ends, and the queue's summary shows the member as
  `waiting-limit (active)`. A member whose session is gone cannot hold the queue.
- **A review round** a reviewer's limit degraded (Kimi in `kimi-mixed`, say) is neither accepted as
  degraded nor counted toward the review cap. `wb.py review-round` decides `limit`, and the planner
  reruns the same round after `limitRetryMinutes` in its visible `#N revmux r<K>` session, which counts
  down on screen. With `"reviewOnLimit": "fallback"` it reruns at once with `claude-only` instead.

What you see: nothing, unless something needs you. The one exception is a limited pane nobody can
type into (a shell, a dialog, a draft in the composer). After `limitRetryMinutes` of that, the relay
sets it blocked with a sound. A Codex "Approaching rate limits" chooser is not waited out, since
nobody answers it; it fails over as above. `-WaitOnLimit` changes nothing else: without it, limits fail
over exactly as before.

The `kimi` label selects the issues. Triage sets it when a product's triage entry has
`"kimiLabel": true` (see Issue triage), and `-Watch -Prune` drops a pending member that lost it. When
a scan finds nothing left to start (no pending or launching member), a watching queue says so once:
`(kimi) idle: no issues left for where: kimi AND priority IN [P2, P3]`, and again only after a new
member came and went (#82). It is a line in the conductor's pane, not a notification. With
Kimi as the implementer, the planner also writes a stricter, Kimi-grade plan (exact edits and what
must not change, an oracle by path or `STOP AND REPORT`, tests first, pitfalls, and what done means,
checked by `wb.py plan-check`). It compares Kimi's diff with that plan before each review round.
Keep Claude reviewers in the roster (`kimi-mixed` does), and never merge on Kimi's own report.

### Auto-merge (opt-in)

`github-workbench <issue> -AutoMerge`, `-Queue <spec> -AutoMerge`, or `"autoMerge": true` in
`~/.agworkbench.json` lets the planner merge its own PR. It does so only when all of these hold:
- the last review round is clean, with every finding fixed or disputed with evidence and none
  deferred - or review **stopped** at a round with no Major (below), and its remaining minors are
  filed as follow-ups;
- the whole suite passed on the PR's head commit;
- `wb.py merge-check --pr <N> --head <sha>` prints `ok`.

Review stops once a verified revmux round has no Blocker, Critical or Major finding (#64): that
round's fix is the last, its Minor findings are fixed if cheap and otherwise go to the PR's
"Leftovers from #N" issue, and no further revmux round runs. The planner records each round with
`wb.py review-round --round K`, which prints `continue`, `stop`, `clean`, `cap` (at or past the review cap), or
`limit` (a `-WaitOnLimit` checkout whose reviewer hit its usage limit: the same round is rerun, and it never
counts toward the cap; see A slow queue that waits out usage limits).
The review cap is `review.maxRounds` (5), or `review.maxRoundsBig` (10) for a big issue (#75): its title
starts with `Batch:`, it has a `batch` or `big` label, its diff against the base passes
`review.bigDiffLines` (1500 lines added + deleted) when a round is recorded, or its checkout was launched
with `-BigReview` (`-Queue <spec> -BigReview` for every member). An issue judged big stays big, by the flag
too: `-NoBigReview` clears the flag for later launches, but once `wb.py settings` or `review-round` has
seen the issue big it keeps the big cap. `wb.py settings` prints `reviewCap=<n>` and why, e.g.
`reviewCap=10 (big: diff 1623 lines > 1500 vs origin/main)`.
A degraded round never stops review. The planner files a stop's deferred minors right after it
opens the PR, whether or not auto-merge is on. `wb.py review-round --summary` prints the line the PR body and
the merge note carry, e.g. `review stopped: round 2 had no Major; 3 minor finding(s) in <URL>`.

That check is read-only. It requires:
- the PR is open, mergeable and `CLEAN` (`UNKNOWN` is retried once);
- no review requests changes;
- there is no unread mail from you (`human`) or from GitHub;
- the relay has seen the PR open;
- the PR head is the tested commit;
- the newest revmux report has a recorded decision, and the last one is not `continue`, nor a `limit`
  (rerun it and record it again), nor a `cap` (a
  Major or a degraded run at or past the review cap) unless `stopWhenNoMajor` was off; after any `stop`,
  every recorded follow-up is filed;
- **no hold**: a label (`do-not-merge`, `hold`, `wip`), the title, or any unmarked description,
  comment, review or line comment containing `hold`, `wait`, `waiting`, `wip`, or `do not merge` in
  any spelling (`don't`, `dont`, `do-not-merge`, typographic apostrophes, markdown emphasis, any
  case) holds the PR at any age. So does a negated lift like "don't go ahead". A hold is lifted only
  by **its own author**, later, with a comment that is nothing but `go ahead`, `resume` or `unhold`
  (optionally `@someone` first, `please` or `!` after). Bots never lift a hold.

Claude posts with your GitHub account, so it ends everything it writes on GitHub with
`<!-- agworkbench:planner -->`. merge-check treats every body without that marker as yours. Hold
words fail safe: "I'll wait for CI" holds too. The merge is `gh pr merge --merge --delete-branch
--match-head-commit <sha>`, so GitHub refuses it if anything was pushed after the tests ran. A
comment on the PR then states each checked condition.

**Keeping the PR mergeable (#32).** With auto-merge on, the planner handles the ordinary reasons
a clean-reviewed PR cannot merge yet itself. merge-check names each one:

| line | what happens |
|---|---|
| `ci-pending:` | Checks are still running, required or optional (a check that never started counts too); a failure is reported only once nothing runs. The relay mails the planner when CI on the head finishes (a `ci` mail, below), and the check runs again. `wb.py wait-ci` can also wait in the background, but it is optional. It never counts a head without any check yet as done: a repo with no CI at all is "done" only after 5 minutes of no checks. |
| `ci-failed:` | A required check failed. A GitHub Actions run is rerun once (`wb.py ci-rerun`). If it is still red, one fix round follows, with the failed jobs' log (`wb.py ci-log`) as evidence. If it is still red after that, it's yours. |
| `behind:` / `conflict:` | An **UPDATE round**. The planner fetches, and the implementer merges exactly that base commit into the branch with `git merge --no-ff`: never a rebase, never a force-push, and nothing else in the merge. It runs the whole suite. `wb.py update-check` then proves the result is one merge commit of that base onto the reviewed head, with a clean tree. If `git show --remerge-diff` is empty, the merge is clean. If not, the resolution is reviewed like a fix, and update-check calls the conflict **small** or **counted** (#90). |
| `ci-optional-failed:` and everything else | Final: the PR waits for you. |

**The relay's CI mail (#94).** A background `wait-ci` dies when Claude Code kills shells under memory
pressure, and then nothing woke the planner when CI finished. So the relay watches CI on each PR poll,
from the check rollup it already reads. Once every check on the PR head has finished, it mails the
planner, from `relay` with kind `ci`: `CI finished on <sha7>: N passed, M failed (names)`. The body has
the full head SHA and each failed check's link. The mail goes out with or without auto-merge. Its
counts take every check, so the planner still runs merge-check, which decides which checks are required.
It is sent once per finished run, not once per head. A rerun on the same head (`wb.py ci-rerun`) needs a
second result. A check that registers late, after the others finished, also changes the result, so
"once per head" would stay silent then. The mail's fixed id and the saved run key mean a relay restart
never repeats it. A head with no check at all gets one mail, `no checks reported in 5 min`, after 5
minutes with no check (wait-ci's no-CI grace). A check stuck pending (an offline runner, a status that never reports) gets no
`ci` mail. Once the pending checks on the head have been unchanged for 90 minutes, wait-ci's timeout,
the stall watch no longer counts the loop as waiting on CI, and its pointer names the stuck checks.

Only the required checks count when branch protection names any; otherwise every check that ran
counts. The limits are kept in code (`wb.py merge-round`, per PR): 3 clean catch-ups, 3 counted
conflict rounds (`mergeRounds.conflict`), 1 CI rerun and 1 CI fix round. Anything beyond them, or
`CANNOT-RESOLVE` from the implementer, goes to you. Without auto-merge none of this runs: the lines
are only reported.

A **small** conflict is not counted at all (#90), so a long-running PR on a busy main is not stopped
by a one-line import clash. A conflict is small when it meets all three conditions:
- it has at most 3 conflict hunks (`mergeRounds.smallConflictHunks`; 0 turns small conflicts off);
- the merge edits or removes no line outside them (code git merged cleanly), adds no line apart
  from them, has no modify/delete or binary conflict, and leaves no marker in the result. A line
  added right beside a conflict cannot be told apart from its resolution;
- no conflicted file is one that the PR's review flagged: a revmux finding's location, or a recorded
  follow-up's file.

`update-check` decides this from the remerge-diff and records it for that merge commit.
`merge-round --kind conflict` reads the record, so the planner never declares a conflict small itself.
Each merge commit is counted once. The merge note carries `wb.py merge-round --pr <N> --summary`, for
example `merge rounds: 1 clean update; conflicts: 2 small (uncounted), 1 counted of 3`.

If any condition fails, the reasons go on the PR and in chat, and the PR waits for you as usual.
The choice is saved per checkout like the implementer: a rerun without the switch keeps it,
`-NoAutoMerge` turns it off (even while the agents are running), and a queue saves either switch and
passes it to the members it launches from then on. To stop a member that is already running, rerun
`github-workbench <n> -NoAutoMerge` for its checkout. The conductor never merges; a member's planner does, and the queue then
records the member as merged.

The per-issue clone gets a Codex trust entry in `~/.codex/config.toml` — the same entry Codex
writes when you answer "Yes" to its trust prompt — because the relay will not answer that prompt
for you, and the loop would otherwise stop on it for every new issue. Only clones this tool creates
are trusted.

### Stalls: the relay notices a loop that went quiet

A loop can sit idle with nothing to wake it. The planner's background mail waiter may be killed
under memory pressure, a helper may finish with nobody watching its result, or the implementer may
stop to ask you a question. Each of these looks exactly like a loop waiting correctly. So on the same
30 s reads the relay also watches for a **stall**. A loop is stalled when both agent panes are
provably idle (no turn running, an empty composer), no mail is unread, and no helper is running.
Mail to a Kimi implementer whose pane shows its usage limit does not count as unread: ringing it
woke nothing, so the stall period still runs. With `-WaitOnLimit` the relay then waits the limit out;
otherwise the stall pointer names the limit and the held mail (#88).
It is not stalled when it is done, when it records that it waits on you (`wb.py status blocked`
writes `.workbench/state/waiting.json`; loop.json `blocked` or `pr-open`), when a PR is open for
your review (outside auto-merge), when an auto-merge PR still has a check running (the planner waits on
CI), or during a usage-limit episode. A helper without its completion marker counts as running while its pane
changes. After two periods of silence it no longer does, and the pointer names it. Your revdiff always counts.

- After `stallMinutes` (default 15) the relay mails the planner one pointer (from `relay`, kind
  `stall`). The pointer quotes the implementer's last line, and the CI result on the open PR's head
  in a line `CI on the PR head: ...` (#94). As a defensive backstop it sends the `ci` mail instead
  if none named that result.
- After two more periods with no progress, and both panes idle for a whole period, it reports the
  loop blocked: a blocked sound status and a notification, waiting.json, and `loop-state blocked` with
  a reason starting `stalled:` in queue mode.
- Progress resets the clock: a commit, any mail, a helper starting or finishing, a loop report.

It only mails, and it never answers a prompt. The sidebar status is not used: agwinterm's agent
hooks rewrite it on every turn.

### Exited agents: the relay restarts a pane that fell back to its shell (#98)

An agent that exits or crashes leaves its pane at the root pwsh prompt. Its mail is held (a shell is
not an agent), and the stall watch only sees a pane that is not idle, so the loop used to sit there
until someone noticed. On the same 30 s reads the relay now looks for it in every state of the loop.
A pane counts as exited when agwinterm reports a live root shell with no child in it
(`foregroundShells`) and its last row is a bare `PS X:\...> ` prompt. The rows above are not read,
since a crash leaves the agent's frame there.

- If the pane stays unchanged for 2 minutes, the relay types the pane's pinned restore command
  into it, the one agwinterm would run on restart. Every pin resumes the conversation: `pane-claude.ps1`,
  `pane-implementer-claude.ps1`, and `-Resume` for Codex and Kimi. The relay logs `agent exited;
  restarted with resume: <box> (<tool>) attempt k/3`. Anything typed at the prompt, or any new
  output, restarts the 2 minutes, so a human who quit the agent on purpose is not fought.
- Once the agent's composer is idle and empty, the relay types one pointer, `Chat from Workbench:
  your agent process exited and the relay restarted it; continue where you left off ...`. A Claude
  pin already starts a resume turn, so for Claude this pointer is a second, short turn. If the pane
  is not idle within 10 minutes, the pointer is dropped and logged.
- The relay restarts a pane at most 3 times an hour. The relay.json records the restarts, so a
  relay restart keeps the count. On the 4th exit the human is alerted: the planner's pane gets the
  blocked sound status and a notification, `waiting.json` is written, and in queue mode the loop is
  reported `loop-state blocked` with cause `environment`, so it keeps its slot. When the dead pane
  is the implementer, the planner also gets mail from `relay` (kind `exit`). To fix it, restart the
  agent with the pane's pinned command, then run `wb.py status active`.
- A pane with no pinned command is not typed into: the relay alerts the same way, once.
- In these cases the relay leaves the pane alone and logs why, once:
  - `restartExited: false` is set in `~/.agworkbench.json`. Any spelling of the key set to anything
    but true or null turns it off, and so does an unreadable config.
  - A launcher holds `launch.lock`, which includes a `-Failover`.
  - The loop is done, or the PR is finished and its close owns the panes.
  - A usage-limit episode owns the pane.
  - `state/implementer.json` records the implementer's tool as limited, or names a different tool
    than the one the relay rings. This happens when a failover did not finish.

**Suites and builds finish visibly.** `wb.py suite --label <sha7> -- <command>` runs a long command
in its own `#N suite <label>` session in direct mode (`lib/run_helper.py`). The command runs with
no shell: its first word is found on PATH, a `.ps1` runs under pwsh, and a `.cmd` or `.bat` shim
(npm, yarn, gradlew, mvn) runs through `cmd /d /s /c`. Because cmd.exe reparses those arguments, one
containing a cmd metacharacter (`& | < > ^ % ! " ( )`) is refused before the session opens; wrap such a
command yourself, e.g. `-- pwsh -NoProfile -Command "..."`. The helper waits for the command, not for
processes it left behind: once the command exits, output still held open by a child (a build server,
a detached test server) is read for 5 s and then left unread. Its output goes to the pane and, as UTF-8 without a BOM, to
`.workbench/review/suite-<label>.log`, even when the command writes UTF-16. When it ends, it mails
the result to the caller's box from `helper`: exit code, failure count, and the log's tail. Its last act is to write a completion marker with both and
the result mail's id and box, so the relay can close it once that mail has been read (#84). revmux and revdiff rounds that fail also mail the planner
("ended without a report") instead of ending silently.

## Configuration

`~/.agworkbench.json` (created by the installer; all keys optional):

| key | default | meaning |
|---|---|---|
| `claudeArgs` | `[]` | extra arguments for `claude` — `-Bypass` puts `--dangerously-skip-permissions` here |
| `codexArgs` | `[]` | extra arguments for `codex`; anything touching the sandbox policy is refused |
| `checkoutRoot` | `~/source/workbench` | where per-issue clones go |
| `allowNetwork` | `false` | let Codex's sandbox reach the network (package installs, tests that fetch); with a Claude implementer, allows its web tools; with Kimi, drops the launcher's check that Kimi's own `[tools] disabled` turns its web tools off |
| `implementer` | `"codex"` | who runs the right pane (`"codex"`, `"claude"` or `"kimi"`) in a new checkout; an existing checkout keeps its saved tool. `-Implementer` changes it for that checkout (refused while a live agent holds the pane) or sets it for a queue's members |
| `revmuxProfile` | by implementer | revmux profile for review rounds: `comprehensive` with Codex, `claude-only` with Claude, `kimi-mixed` with Kimi when revmux has it (else `claude-only`) |
| `failover` | `true` | when the implementer hits its usage limit, the planner stops it (only when idle at the limit) and switches to the next tool in `failoverOrder`; `false` only reports |
| `failoverOrder` | `["claude", "codex", "kimi"]` | the tools a failover (and a queue with a limited tool) tries, in order: the first that is not the limited one and has no recorded limit; `-Failover` also skips a Kimi that fails its launch checks, while a queue routes by limits only and lets the member's launch check Kimi (a refusal defers the member). At least two distinct tools |
| `kimiPath` | none | `kimi.exe` for the Kimi implementer; without it, `PATH`, then `%USERPROFILE%\.kimi-code\bin\kimi.exe` |
| `kimiArgs` | `[]` | extra arguments for `kimi` (e.g. `["-m", "<model alias>"]`); approval-mode, session, agent and directory flags are refused in every spelling |
| `bugLabel` | `"bug"` | the label `-Queue bugs` stands for (non-empty, no comma) |
| `triage` | none | per product repo: `{"owner/repo": {"specRepos": [...], "model": "...", "kimiLabel": false}}`, the private spec repos `-Triage` judges against, and whether it also sets the `kimi` label (see Issue triage) |
| `followUp` | `{"dedupe": true, "bumpAt": {"P2": 2, "P1": 3, "P0": 5}}` | `dedupe: false` skips duplicate matching: separate items keep #27's filing, leftovers still share one issue per PR (per item without `--pr`); `bumpAt` is the total number of reports that raises a matched issue to each priority |
| `review` | `{"stopWhenNoMajor": true, "minRounds": 1, "maxRounds": 5, "maxRoundsBig": 10, "bigDiffLines": 1500}` | `stopWhenNoMajor: false` keeps reviewing until a round has no findings at all (up to the review cap); `minRounds` (1-5, at most `maxRounds`) is the first round that may stop review; `maxRounds` (1-20) is the review cap, `maxRoundsBig` (`maxRounds`-20, default the larger of 10 and `maxRounds`) the cap of a big issue, and `bigDiffLines` (1 or more) the diff size that makes an issue big |
| `mergeRounds` | `{"conflict": 3, "smallConflictHunks": 3}` | `conflict` (1-10) is the number of counted conflict UPDATE rounds per PR; `smallConflictHunks` (0-20) is the most conflict hunks a small, uncounted conflict may have, and 0 turns small conflicts off (#90) |
| `autonomous` | `false` | full autonomy: merge, file follow-up issues, close the sessions after the merge; implies `autoMerge` |
| `cleanup` | `"merged"` | after an autonomous close: `merged` deletes the checkout when it is safe, `build` deletes only its build outputs, `off` keeps it (see Cleaning up checkouts) |
| `minFreeGB` | `20` | the queue admits no member while the checkout drive has less free space (GiB); `0` turns the guard off |
| `minFreeRamGB` | `3` | the queue admits no member while less memory is free (GiB); `0` turns the guard off |
| `stallMinutes` | `15` | minutes a loop may sit idle with nothing to wake it before the relay mails the planner a stall pointer (see Stalls); `0` turns the watch off |
| `limitRetryMinutes` | `30` | with `-WaitOnLimit`: minutes between the relay's probes of an agent waiting out its usage limit, and the wait before rerunning a review round a reviewer's limit stopped (more than 0; see A slow queue that waits out usage limits) |
| `closeHelpers` | `true` | the relay closes a finished revmux or suite helper session once its result mail has been read (see Finished helpers, before the merge); `false` keeps them until the loop's close |
| `restartExited` | `true` | the relay restarts an agent that exited to its pane's shell with the pane's pinned command, at most 3 times an hour (see Exited agents); `false` leaves the pane alone |
| `reviewOnLimit` | `"wait"` | with `-WaitOnLimit`, a review round a reviewer's usage limit degraded: `wait` reruns it after `limitRetryMinutes`, `fallback` reruns it at once with `claude-only` |
| `autoMerge` | `false` | new checkouts let the planner merge its own PR when every auto-merge condition holds; `-AutoMerge` / `-NoAutoMerge` change it per checkout or queue |

## Layout

```
github-workbench.cmd        the command: works in cmd and PowerShell
install.ps1                 prerequisites, the Claude command, the Codex skill, PATH
lib/github-workbench.ps1    terminal detection, clone, session, split, relay
lib/pane-claude.ps1         left pane: claude "/start-github-issue <issue>"
lib/pane-codex.ps1          right pane: codex, sandboxed, with the implementer prompt
lib/pane-implementer-claude.ps1  right pane with implementer=claude: claude "/workbench-implementer <issue>"
lib/pane-implementer-kimi.ps1    right pane with implementer=kimi: kimi --yolo, with its role and guard rails (#65)
lib/kimi.py                 the Kimi pane's role file, git/gh shims, web-tool check and trust record
kimi/AGENTS.md, kimi/bin/   the Kimi implementer's role and its shell shims (templates)
lib/relay.py                mail doorbell and PR watcher; spots usage limits and stalls in the agent panes
lib/limits.py               recognises an agent's own usage-limit message in a pane frame (#24)
lib/closer.py               the autonomous close after a merge, shared by the relay and the conductor (#27, #33)
lib/cleanup.py              deletes finished checkouts: after an autonomous close, and -Cleanup (#41)
lib/triage.py               priority labels for a product repo's issues, from its private spec repos (#34)
lib/labelquery.py           the boolean label query behind -Queue 'where: ...' (#38)
lib/helper_done.py          a helper session's completion marker (#33)
lib/run_helper.py           a long command (the suite, a build) in its own session: UTF-8 log, marker, mail (#45)
lib/run-revmux.ps1          one review round, report posted to Claude
lib/human-review.ps1        revdiff for you, annotations posted to Claude
lib/wb.py                   opens those sessions for Claude with correct Windows paths
lib/agmsg.py, hub.py,       the mailbox and the fail-closed pane messenger, vendored from the
    agw.py, peerchat.py     tested ai-hub tooling
claude/commands/start-github-issue.md      the loop, from Claude's side
claude/commands/workbench-implementer.md   the loop, from the implementer's side when it is Claude
claude/commands/triage-issue.md            one issue's priority judgment; triage.py runs it headless
codex/skills/workbench-implementer/        the loop, from Codex's side
tests/                      python -m unittest discover -s tests   (no terminal needed)
```

## Credits

The layout, the peer-chat mechanics and the principle that the value is disagreement are
umputun's, from agterm's `two-agent-chat` recipe. revmux and revdiff are his too. agwinterm is the
Windows terminal that makes the rest possible.

MIT licence.
