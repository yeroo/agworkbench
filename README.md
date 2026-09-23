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

The launcher pins restart commands for Claude, Codex and the relay, and confirms that agwinterm's
`restore-commands` setting is enabled. A failed setting or pin stops setup with repair instructions.
Review helpers are not pinned. Restarting agwinterm with its session tree intact replays these
commands; running the launcher again refreshes the pins and leaves non-shell panes untouched.

Claude's conversation ID, original project directory and pane binding live in
`.workbench/state/claude.json`. The launcher reserves the ID before starting Claude, so an
interrupted first launch can retry with the same ID. The pane script resumes that exact
conversation when its transcript exists, or starts the reserved conversation otherwise. A
replacement Claude pane gets a new reservation and the old record is archived. When adopting a
running Claude, its `CLAUDE_CODE_SESSION_ID` and transcript must be available; a missing identity
is refused before adoption. `claudeArgs` cannot override conversation identity or launch mode.

Codex's restart command uses `-Resume` to select the newest interactive rollout for the checkout
by its metadata timestamp, ignoring review/exec rollouts and other directories. It reapplies the
same sandbox, approval and environment policy. If no matching rollout exists, it starts fresh.
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
ring, which raises an alert after a minute; the background waiter still wakes Claude. Codex uses
the relay's queued pointers. Claude stops its waiter when the loop is complete.

## Safety model

**Codex** runs `--sandbox workspace-write --ask-for-approval never`, rooted at the issue's own
clone. Network access and writes outside the clone are blocked, and the two settings that tune the
sandbox are pinned on the command line so a config file cannot loosen them. `"allowNetwork": true`
in `~/.agworkbench.json` is the one way to open the network. Nothing in `codexArgs` may re-decide
the policy — not by flag, config key, profile, or alias; the test suite pins every spelling.

Because its sandbox denies the terminal's control pipe, Codex cannot type into any pane — and
doesn't need to: it writes mail files inside its clone, and the relay rings Claude.

**The relay** types only into the two panes of its own session, only into an agent's composer it
can prove is empty, never into a dialog, and never answers a prompt. A refusal before typing waits
for the next tick. After typing a pointer once, it verifies submission from an empty composer
and records whether the pointer was submitted or queued. A stuck pointer gets up to two more
submit-key presses, each guarded by a fresh composer check. A clipped pointer must still show its
complete message ID. Codex gets one Return fallback only
when no running-turn or queued-input evidence is visible. It never submits a changed draft or
a dialog. This verifies submission, not that the agent has read the mail. Failed submissions
alert immediately; mail held for a minute also raises a blocked status, sound, blink, and desktop
notification naming the recipient, message, and reason. Alerts repeat at most every five minutes
per message. Failed rings stay unannounced and can be sent again once the composer is empty.
When alerted mail is delivered or independently read, the relay clears its last outstanding alert
for that recipient to idle. That reset can race a newer agent-hook status; avoiding the race
would require terminal support for conditional status ownership.
After merge or closure, the relay keeps delivering the final notices and alerting on holds for
up to 30 minutes, then logs any notices still waiting before exiting. A failed status reset is
persisted and retried on later ticks, including after a restart. Relay dry runs neither file
GitHub-event mail nor save announcement state.

The relay follows the newest open PR from the watched repository and branch. A PR already
finished before the relay observed it open is recorded and ignored. OPEN observation survives
restarts: if that PR finishes while the relay is offline, its merge or closure still ends the
loop after the final notices drain. Once those notices and status resets are resolved, the PR
is retired; restarting on the same branch waits for the next open PR. Pending final notices
from older relay versions are also drained once for compatibility.

**Nobody merges but you.** Claude may push the branch and open the PR; it never approves its own PR,
never merges, never force-pushes over commits you have reviewed.

The per-issue clone gets a Codex trust entry in `~/.codex/config.toml` — the same entry Codex
writes when you answer "Yes" to its trust prompt — because the relay will not answer that prompt
for you, and the loop would otherwise stop on it for every new issue. Only clones this tool creates
are trusted.

## Configuration

`~/.agworkbench.json` (created by the installer; all keys optional):

| key | default | meaning |
|---|---|---|
| `claudeArgs` | `[]` | extra arguments for `claude` — `-Bypass` puts `--dangerously-skip-permissions` here |
| `codexArgs` | `[]` | extra arguments for `codex`; anything touching the sandbox policy is refused |
| `checkoutRoot` | `~/source/workbench` | where per-issue clones go |
| `allowNetwork` | `false` | let Codex's sandbox reach the network (package installs, tests that fetch) |

## Layout

```
github-workbench.cmd        the command: works in cmd and PowerShell
install.ps1                 prerequisites, the Claude command, the Codex skill, PATH
lib/github-workbench.ps1    terminal detection, clone, session, split, relay
lib/pane-claude.ps1         left pane: claude "/start-github-issue <issue>"
lib/pane-codex.ps1          right pane: codex, sandboxed, with the implementer prompt
lib/relay.py                mail doorbell and PR watcher
lib/run-revmux.ps1          one review round, report posted to Claude
lib/human-review.ps1        revdiff for you, annotations posted to Claude
lib/wb.py                   opens those sessions for Claude with correct Windows paths
lib/agmsg.py, hub.py,       the mailbox and the fail-closed pane messenger, vendored from the
    agw.py, peerchat.py     tested ai-hub tooling
claude/commands/start-github-issue.md      the loop, from Claude's side
codex/skills/workbench-implementer/        the loop, from Codex's side
tests/                      python -m unittest discover -s tests   (no terminal needed)
```

## Credits

The layout, the peer-chat mechanics and the principle that the value is disagreement are
umputun's, from agterm's `two-agent-chat` recipe. revmux and revdiff are his too. agwinterm is the
Windows terminal that makes the rest possible.

MIT licence.
