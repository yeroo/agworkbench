#!/usr/bin/env python3
"""wb - open the workbench's visible helper sessions without composing terminal commands by hand.

  wb.py revmux --round 1 --scope .workbench/review/scope-r1.md    # review round, own session
  wb.py human-review --base origin/main                          # revdiff, selected, for the human
  wb.py suite --label 1f04542 -- python -m unittest discover -s tests   # the whole suite, own session (#45)
  wb.py status blocked --sound                                    # this pane's sidebar status
  wb.py wait-mail                                                 # background inbox waiter
  wb.py settings                                                  # implementer, revmux profile, auto-merge, failover
  wb.py handover                                                  # open request, branch, git status (#24)
  wb.py follow-up add --key r2-m1 --title T --severity minor --origin "review r2"   (#27)
  wb.py follow-up file --source 27 --pr 30                        # file every unfiled follow-up (#27), deduped (#42)
  wb.py loop-state done --pr 30 --sha <sha>                       # the planner's last act (#27)
  wb.py loop-state done --no-pr --reason "duplicate"               # closed issue needing no change (#53)
  wb.py loop-state blocked --environmental --reason "codex limited" # a block the human cannot answer (#61)
  wb.py loop-state blocked --needs-human --reason "GitHub API rate limit"  # -WaitOnLimit: not an agent limit (#88)
  wb.py wait-limit --reason "kimi 5-hour limit"                   # -WaitOnLimit: the relay waits it out (#88)
  wb.py review-round --round 2                                    # a verified revmux round's decision (#64)
  wb.py review-round --summary                                    # why review ended, for the PR body / merge note
  wb.py merge-check --pr 12 --head <sha>                          # read-only auto-merge gate (#23)
  wb.py wait-ci --pr 12 --head <sha>                              # background: until CI on the head is done (#32)
  wb.py update-check --reviewed <sha> --base <sha>                # an UPDATE round is one merge of the base (#32)
  wb.py merge-round --pr 12 --kind update                         # count a proved round; refuse past the limit (#32)
  wb.py merge-round --pr 12 --summary                             # the merge note's line of the PR's rounds (#90)
  wb.py ci-rerun --pr 12                                          # rerun the failed Actions jobs once (#32)
  wb.py ci-log --pr 12                                            # the failed jobs' log for a FIX round (#32)
  wb.py route-issue 109 --repo yeroo/agworkbench                  # which roster entry (tool + model) works it (#109)

Why a helper: Claude's shell is Git Bash, where $PWD is a POSIX path (/c/Users/...) that PowerShell
cannot use, and quoting a PowerShell command inside a bash string inside an agwintermctl argument
is three quoting languages deep. Everything here is derived from AI_HUB, which the workbench set.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import agw  # noqa: E402
import followup  # noqa: E402
import hub  # noqa: E402
import limits  # noqa: E402
import route  # noqa: E402
import triage  # noqa: E402


def checkout() -> Path:
    hub = os.environ.get("AI_HUB")
    if not hub:
        raise SystemExit("wb: AI_HUB is not set - run this inside an agworkbench pane")
    return Path(hub).resolve().parent


def current_branch(root: Path) -> str:
    return subprocess.run(["git", "-C", str(root), "branch", "--show-current"],
                          capture_output=True, text=True).stdout.strip()


def issue_number(root: Path, branch: str | None = None) -> str:
    branch = current_branch(root) if branch is None else branch
    match = re.match(r"issue-(\d+)", branch)
    return match.group(1) if match else "?"


# agwinterm's limits on a direct-mode session.new command (Agwinterm.Pty/SessionCommand.cs, shared with
# Lite's session_command.h): the app under 260 UTF-8 bytes, at most 16 arguments after it, each under
# 2048 bytes. Past them the host creates nothing (#86).
HOST_APP_BYTES, HOST_ARGS, HOST_ARG_BYTES = 260, 16, 2048
HOST_LIMITS = "app 259 bytes, 16 arguments, 2047 bytes each"


def utf8_size(text: str) -> int:
    return len(text.encode("utf-8", "surrogatepass"))


def fits_host(argv: list[str]) -> str | None:
    """Why agwinterm would refuse this argv as a session command, or None when it fits."""
    if not argv:
        return "no command"
    if utf8_size(argv[0]) >= HOST_APP_BYTES:
        return f"the app is {utf8_size(argv[0])} bytes: {argv[0]}"
    if len(argv) - 1 > HOST_ARGS:
        return f"{len(argv) - 1} arguments after the app"
    for arg in argv[1:]:
        if utf8_size(arg) >= HOST_ARG_BYTES:
            return f"an argument is {utf8_size(arg)} bytes: {arg[:80]}..."
    return None


def launch_file(root: Path, name: str) -> Path:
    """Where a helper's launch file goes: beside the helpers' .done markers, which are the only
    files there anything globs."""
    directory = root / ".workbench" / "state" / "helpers"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / name


def ps_quote(value: str) -> str:
    """A PowerShell single-quoted literal. PowerShell takes the typographic single quotes as quotes
    too, so they are doubled like '."""
    return "'" + re.sub("(['\u2018\u2019\u201a\u201b])", r"\1\1", value) + "'"


def pane_command(root: Path, tag: str, script: str, **params: str) -> list[str]:
    """A helper's command for agwinterm's direct mode: no shell around it (#33). When the helper
    ends, its pane stays on screen with its input closed, so the close can prove it untouched
    (closer.py). The parameters go into a launch file, so the command stays short whatever they
    are (#86): still one PowerShell process, the launcher calling the script."""
    call = " ".join([f"& {ps_quote(str(HERE / script))}", *(f"-{key} {ps_quote(value)}" for key, value in params.items())])
    launcher = launch_file(root, f"launch-{tag}.ps1")
    # `exit $LASTEXITCODE` passes on the script's own `exit N`; a script that ends without one leaves
    # its last native command's code instead of 0. Nothing reads a helper pane's exit code.
    # The BOM makes Windows PowerShell 5.1 read it as UTF-8; newline="" keeps a value's own newlines.
    launcher.write_text(f"# wb.py {tag} (#86)\n{call}\nexit $LASTEXITCODE\n", encoding="utf-8-sig", newline="")
    shell = shutil.which("pwsh") or shutil.which("pwsh-preview") or shutil.which("powershell.exe") or "powershell.exe"
    return [shell, "-NoLogo", "-ExecutionPolicy", "Bypass", "-File", str(launcher)]


def helper_command(root: Path, tag: str, script: str, *args: str) -> list[str]:
    """A Python helper's command for direct mode (#45): python itself runs in the pane, so when the
    helper ends no shell is left in its foreground and the close can prove it untouched. Its
    arguments go into a launch file it reads first (#86)."""
    arguments = launch_file(root, f"launch-{tag}.json")
    arguments.write_text(json.dumps(list(args), ensure_ascii=False), encoding="utf-8")
    return [sys.executable or "python", str(HERE / script), "--args-file", str(arguments)]


def open_session(name: str, cwd: Path, argv: list[str], select: bool) -> str:
    refusal = fits_host(argv)
    if refusal:
        raise SystemExit(f"wb: helper command exceeds agwinterm's session.new limits ({HOST_LIMITS}): {refusal}")
    line = shlex.join(argv) if agw.use_agterm() else subprocess.list2cmdline(argv)  # sh under agterm (#60)
    args = {"name": name, "cwd": str(cwd), "command": line, "command-mode": "direct"}
    pane = agw.my_pane()
    found = agw.find_pane(pane, agw.tree()) if pane else None
    workspace = found[0].get('id') if found else None
    if workspace:
        args['workspace'] = workspace
    else:
        print('wb: caller workspace not found; opening helper in the active workspace', file=sys.stderr)
    if not select:
        args["no-select"] = True
    result = agw.request("session.new", args=args)
    return str(result).split()[0] if result else ""


def revmux_profile(root: Path) -> str:
    """The profile the launcher resolved for this checkout (#20): claude-only when Claude or Kimi
    (#65) is the implementer and Codex may be out of quota, comprehensive otherwise, or the human's
    revmuxProfile."""
    return checkout_settings(root)["revmuxProfile"]


def cmd_revmux(args: argparse.Namespace) -> int:
    root = checkout()
    if args.after is not None and not args.rerun:
        raise SystemExit("wb: revmux --after goes with --rerun")
    if args.after is not None and args.after < 1:
        raise SystemExit("wb: revmux --after must be 1 or more minutes")
    params = {}
    scope_arg = args.scope
    renames: list[tuple[Path, Path]] = []
    if args.rerun:
        # A round a reviewer's usage limit stopped (#77): same K, a new revmux run name - revmux refuses a
        # round that has already run - and the limited report kept beside it. The attempt counts every
        # limited file, so a rerun that died before its report still gets a fresh run name.
        review = root / ".workbench" / "review"
        report = review / f"revmux-r{args.round}.md"
        earlier = [int(match[1]) for path in review.glob(f"revmux-r{args.round}-limited-*")
                   if (match := re.fullmatch(rf"revmux-r{args.round}-limited-(\d+)\.(?:md|json)", path.name))]
        if not report.is_file() and not earlier:
            raise SystemExit(f"wb: revmux --rerun: no report to rerun: {report}")
        # The current record's run counts too: a rerun that decided `limit` again left r<K>-<n> in it.
        current = read_run_record(root, args.round) or {}
        used = [current["attempt"]] if type(current.get("attempt")) is int else []
        if match := re.fullmatch(rf"r{args.round}-(\d+)", str(current.get("run") or "")):
            used.append(int(match[1]))
        attempt = max(earlier + used, default=0) + 1
        # The scope: this round's run record, else the newest limited one (a rerun that died early).
        records = [read_run_record(root, args.round)]
        if earlier:
            try:
                records.append(json.loads((review / f"revmux-r{args.round}-limited-{max(earlier)}.json")
                                          .read_text(encoding="utf-8-sig")))
            except (OSError, ValueError):
                pass
        if scope_arg is None:
            scope_arg = next((record["scope"] for record in records
                              if isinstance(record, dict) and isinstance(record.get("scope"), str)), None)
        if report.is_file():
            renames.append((report, review / f"revmux-r{args.round}-limited-{attempt}.md"))
            if run_record_path(root, args.round).exists():
                renames.append((run_record_path(root, args.round),
                                review / f"revmux-r{args.round}-limited-{attempt}.json"))
        params.update(Run=f"r{args.round}-{attempt}", Attempt=str(attempt))
        if args.after is not None:
            params["After"] = str(args.after)
    if scope_arg is None:
        raise SystemExit("wb: revmux needs --scope (a rerun reuses the limited run's scope when it was recorded)")
    scope = (root / scope_arg).resolve() if not Path(scope_arg).is_absolute() else Path(scope_arg)
    if not scope.is_file():
        raise SystemExit(f"wb: scope file not found: {scope}")
    tag = f"revmux-r{args.round}" + (f"-{params['Attempt']}" if "Attempt" in params else "")
    command = pane_command(root, tag, "run-revmux.ps1", Checkout=str(root), ScopeFile=str(scope),
                           Round=str(args.round), Profile=args.profile or revmux_profile(root), **params)
    # The limited report is set aside last, and put back when the session does not start.
    done: list[tuple[Path, Path]] = []
    try:
        for source, target in renames:
            source.rename(target)
            done.append((source, target))
        sid = open_session(f"#{issue_number(root)} revmux r{args.round}", root, command, select=False)
    except BaseException:
        for source, target in reversed(done):
            target.rename(source)
        raise
    wait = f" after a {args.after}-minute wait for the reviewer's usage limit" if args.after else ""
    print(f"revmux round {args.round} running in session {sid}{wait}; the report will arrive as mail")
    return 0


def cmd_human_review(args: argparse.Namespace) -> int:
    root = checkout()
    command = pane_command(root, "human-review", "human-review.ps1", Checkout=str(root), Base=args.base)
    sid = open_session(f"#{issue_number(root)} your review", root, command, select=True)
    print(f"human review open in session {sid}; annotations will arrive as mail from 'human'")
    return 0


def cmd_suite(args: argparse.Namespace) -> int:
    root = checkout()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not re.fullmatch(r"[A-Za-z0-9._-]+", args.label):
        raise SystemExit(f"wb: --label must be letters, digits, '.', '_' or '-' (got {args.label!r})")
    if not command:
        raise SystemExit("wb: suite needs a command after --, e.g. wb.py suite --label abc1234 -- python -m unittest")
    # The session does not inherit this pane's environment, so the recipient is resolved here.
    import run_helper
    refusal = run_helper.shim_refusal(command)
    if refusal:
        raise SystemExit(f"wb: suite refused: {refusal}")
    to = args.to or os.environ.get("AI_BOX") or "claude"
    if not hub.BOX_RE.fullmatch(to):
        raise SystemExit(f"wb: --to is not a mailbox name: {to!r}")
    hub_dir = root / ".workbench"
    line = helper_command(root, f"suite-{args.label}", "run_helper.py", "--hub", str(hub_dir), "--label", args.label, "--to", to, "--", *command)
    sid = open_session(f"#{issue_number(root)} suite {args.label}", root, line, select=False)
    print(f"suite {args.label} running in session {sid}; log: {hub_dir / 'review' / f'suite-{args.label}.log'}; "
          f"the result will arrive as mail from 'helper' to {to}")
    return 0


def waiting_path(root: Path) -> Path:
    return root / ".workbench" / "state" / "waiting.json"


def set_waiting(root: Path, waiting: bool) -> None:
    """The durable "waiting on the human" record (#45). The sidebar status cannot be it: agwinterm's
    agent hooks rewrite that on every turn (active, then completed at the end of it). The relay's stall
    watch is off while this file exists."""
    path = waiting_path(root)
    if waiting:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"at": time.time(), "by": "planner"}), encoding="utf-8")
    else:
        path.unlink(missing_ok=True)


def cmd_status(args: argparse.Namespace) -> int:
    hub_dir = os.environ.get("AI_HUB")
    if hub_dir:
        set_waiting(Path(hub_dir).resolve().parent, args.state == "blocked")
    agw.set_status(args.state, sound=args.sound, blink=args.sound)
    return 0


# Any limit in a `loop-state blocked` reason (#88), deliberately broad: two review rounds showed that no
# free-text pattern tells an agent's usage limit ("kimi: 5h limit hit") from a real block ("GitHub API rate
# limit", "codex limited; failover is off"). So in a -WaitOnLimit checkout every limit word is a question,
# which the planner answers: `wb.py wait-limit` for an agent's limit, `--needs-human` for anything else.
# The agents' own limit messages (limits.LIMITED) count too.
LIMIT_REASON = re.compile("|".join([r"limit", r"quota", r"credit", r"5[- ]?h(?:ours?)?\b", r"\b403\b",
                                    limits.KIMI_QUOTA,
                                    *(pattern for patterns in limits.LIMITED.values() for pattern in patterns)]),
                          re.IGNORECASE)


def limit_reason(reason: str | None) -> bool:
    return bool(LIMIT_REASON.search(limits.APOSTROPHES.sub("'", reason or "")))


def cmd_loop_state(args: argparse.Namespace) -> int:
    # Reports stay in this checkout; the conductor alone owns the global queue.
    if getattr(args, 'needs_human', False) and args.state != 'blocked':
        print('wb: loop-state: --needs-human is only for blocked', file=sys.stderr)
        return 2
    if (args.state == 'blocked' and not getattr(args, 'needs_human', False) and limit_reason(args.reason)
            and checkout_settings(checkout())["onLimit"] == "wait"):
        # #88: a blocked loop waits for the human, and a -WaitOnLimit loop must resume by itself.
        print('wb: loop-state: this checkout waits out usage limits (onLimit=wait), and the reason names a '
              'limit. If an agent is at its usage limit, run `wb.py wait-limit --reason ...`: the relay waits '
              'it out. If this block really needs the human (a GitHub, CI or disk limit, a Codex warning '
              'chooser that could not fail over), rerun with --needs-human.', file=sys.stderr)
        return 1
    if args.state == 'done':
        code = (loop_done_no_pr(checkout(), args.reason, args.pr) if getattr(args, 'no_pr', False)
                else loop_done(checkout(), args.pr, args.sha))
        if code == 0:
            set_waiting(checkout(), False)
        return code
    from conductor import retire_no_pr_done, write_loop_state
    if getattr(args, 'environmental', False) and args.state != 'blocked':
        print('wb: loop-state: --environmental is only for blocked', file=sys.stderr)
        return 2
    try:
        write_loop_state(checkout(), args.state, args.pr, args.reason,
                         cause='environment' if getattr(args, 'environmental', False) else None)
        if args.state in ('resumed', 'pr-open'):
            retire_no_pr_done(checkout() / '.workbench/state/loop-done.json')
        if args.state == 'resumed':
            set_waiting(checkout(), False)
        return 0
    except (OSError, ValueError, KeyError, TypeError) as err:
        print(f'wb: loop-state: {err}', file=sys.stderr)
        return 2


def cmd_wait_limit(args: argparse.Namespace) -> int:
    """#88: the planner saw an agent's usage limit that the relay did not detect. In a -WaitOnLimit
    checkout the relay turns this request into a wait episode on its next limit check (it deletes the
    file): mail held, a probe every limitRetryMinutes, the loop resuming by itself."""
    root = checkout()
    if checkout_settings(root)["onLimit"] != "wait":
        print("wb: wait-limit: this checkout fails over on a usage limit (onLimit is not wait): follow "
              "start-github-issue.md, Usage limits, and run github-workbench.cmd <issue> -Failover",
              file=sys.stderr)
        return 1
    from conductor import atomic_json
    path = root / ".workbench" / "state" / "limit-request.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(path, {"box": args.box, "reason": args.reason, "at": time.time()})
    print(f"wb: wait-limit: the relay waits out {args.box}'s usage limit from its next check")
    return 0


def checkout_settings(root: Path) -> dict:
    """The checkout's settings record (state/implementer.json, #20 and #23), parsed once. Missing or
    invalid keys read as their defaults: a record written before #23 has no autoMerge, and that is off."""
    try:
        saved = json.loads((root / ".workbench" / "state" / "implementer.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        saved = {}
    if not isinstance(saved, dict):
        saved = {}
    tool = saved.get("tool") if saved.get("tool") in ("codex", "claude", "kimi") else "codex"
    profile = saved.get("revmuxProfile")
    if not (isinstance(profile, str) and re.fullmatch(r"[A-Za-z0-9._-]+", profile)):
        # The launcher's Get-RevmuxProfile default: Codex reviews only when Codex implements. For kimi the
        # launcher probes revmux for kimi-mixed (#66); this fallback cannot, and it only runs when
        # implementer.json is missing, so it stays claude-only, which every revmux has.
        profile = "comprehensive" if tool == "codex" else "claude-only"
    return {"implementer": tool, "revmuxProfile": profile, "autoMerge": saved.get("autoMerge") is True,
            "autonomous": saved.get("autonomous") is True, "bigReview": saved.get("bigReview") is True,
            "onLimit": "wait" if saved.get("onLimit") == "wait" else "failover"}


def failover_setting() -> bool:
    """`failover` from ~/.agworkbench.json (#24): on unless the human set it to false."""
    path = Path(os.environ.get("AGWORKBENCH_CONFIG") or (Path.home() / ".agworkbench.json"))
    try:
        config = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return True
    return not (isinstance(config, dict) and config.get("failover") is False)


def cmd_settings(args: argparse.Namespace) -> int:
    settings = checkout_settings(checkout())
    try:
        review = followup.review_settings(followup.read_config())
        review_line = (f"stopWhenNoMajor={'true' if review['stopWhenNoMajor'] else 'false'} "
                       f"minRounds={review['minRounds']}")
        # Seeing an issue big here is a moment it is judged big (#75): latching only ever raises the cap.
        cap = review_cap(checkout(), review)
        cap_line = f"reviewCap={cap['cap']}" + (f" (big: {cap['reason']})" if cap["big"] else "")
    except followup.SettingsError:
        review_line = "stopWhenNoMajor=invalid minRounds=invalid"    # review-round refuses with the reason
        cap_line = "reviewCap=invalid"
    print(f"implementer={settings['implementer']} revmuxProfile={settings['revmuxProfile']} "
          f"autoMerge={'true' if settings['autoMerge'] else 'false'} "
          f"autonomous={'true' if settings['autonomous'] else 'false'} {review_line} {cap_line} "
          f"failover={'true' if failover_setting() else 'false'} onLimit={settings['onLimit']}")
    return 0


# --- handover (#24) --------------------------------------------------------------------------

def _mail(box: str, sender: str) -> list[dict]:
    """Every message in a box from one sender - unread, read and archived - oldest first."""
    found = []
    base = hub.box_dir(box)
    for folder in (base, base / "read", base / "archive"):
        for path in folder.glob("*.md") if folder.is_dir() else []:
            try:
                message = hub.parse_message(path)
            except (OSError, ValueError):
                continue
            if message.get("from") == sender:
                found.append(message)
    return sorted(found, key=lambda message: message.get("id", ""))


def open_request() -> tuple[dict | None, list[dict]]:
    """The newest planner -> implementer message with no later implementer -> planner reply.
    Message ids start with a UTC timestamp, so they order in time."""
    sent = _mail("codex", "claude")
    replies = _mail("claude", "codex")
    last_reply = replies[-1].get("id", "") if replies else ""
    pending = sent[-1] if sent and sent[-1].get("id", "") > last_reply else None
    return pending, sent[-3:]


def cmd_handover(args: argparse.Namespace) -> int:
    root = checkout()
    hub.reload_paths()
    pending, recent = open_request()
    runs = [subprocess.run(["git", "-C", str(root), *argv], capture_output=True, text=True)
            for argv in (["branch", "--show-current"], ["status", "--short"])]
    for done in runs:
        if done.returncode != 0:
            # A HANDOVER built on a failed git call would call a dirty tree clean.
            print(f"wb: handover: git {' '.join(done.args[3:])} failed: {(done.stderr or done.stdout).strip()}",
                  file=sys.stderr)
            return 1
    branch, status = runs[0].stdout.strip(), runs[1].stdout
    print(f"branch: {branch or '(detached HEAD)'}")
    if pending:
        print(f"open request: {pending.get('id')} \"{pending.get('subject', '')}\" (no reply yet)")
    else:
        print("open request: none (the last message to the implementer was answered)")
    print("last messages to the implementer:")
    for message in recent:
        print(f"  {message.get('id')} {message.get('subject', '')}")
    print("git status --short:")
    print("\n".join("  " + line for line in status.splitlines()) or "  (clean)")
    return 0


# --- merge-check (#23) -----------------------------------------------------------------------
# Read-only: state, reviews, holds and head are pure functions over what one `gh pr view` (plus the
# PR's inline comments) returned, and - only for the tested head in an UNSTABLE or BLOCKED state -
# what `gh pr checks` (all, and --required) returned (#32); mail and relay read this checkout's
# .workbench. The planner merges only on "ok". When in doubt, hold: fail closed.

PLANNER_MARKER = "<!-- agworkbench:planner -->"
NEGATION = r"(?:do[\s-]*not|don'?t|dont)[\s-]*"
HOLD_RE = re.compile(r"\b(?:hold|wait(?:ing)?|wip|" + NEGATION + r"merge|" +
                     NEGATION + r"(?:go[\s-]*ahead|resume|unhold))\b")
# A lift is the whole comment, a bare directive, optionally addressed: "go ahead", "@claude resume.".
LIFT_RE = re.compile(r"(?:@\S+ )?(?:go ahead|resume|unhold)(?: please)?[.!]?")
LABEL_HOLD_RE = re.compile(r"do.?not.?merge|hold|wip")
PR_FIELDS = ("number,url,state,mergeable,mergeStateStatus,reviewDecision,headRefOid,reviews,comments,"
             "labels,title,body,author")


def normalize(text: str) -> str:
    """Lower-case, typographic apostrophes to ', markdown emphasis and code marks dropped,
    whitespace collapsed - so "Do **not**\\nmerge" and "Don’t merge" read as what they say."""
    text = re.sub("[‘’ʼ]", "'", text or "")
    text = re.sub(r"[*_~`]", "", text)
    return " ".join(text.split()).lower()


def _login(item: dict) -> str:
    return ((item.get("author") or item.get("user") or {}).get("login")) or "?"


def _when(item: dict) -> str:
    # ISO-8601 UTC strings from GitHub sort correctly as text.
    return item.get("submittedAt") or item.get("createdAt") or item.get("created_at") or ""


# --- CI and the branch (#32) ----------------------------------------------------------------------
# merge-check classifies what keeps a clean-reviewed PR from merging, so the planner can route the
# ordinary cases itself: `ci-pending:` (wait-ci), `ci-failed:` (one rerun, then one FIX round),
# `behind:` / `conflict:` (an UPDATE round). Everything else keeps the final `mergeable:` prefix.
# CI comes from `gh pr checks`, whose `bucket` normalises check runs and status contexts alike.

CHECK_FIELDS = "name,state,bucket,link,workflow"
FAILED_BUCKETS = ("fail", "cancel")
ROUND_LIMITS = {"update": 3, "conflict": 3, "ci-rerun": 1, "ci-fix": 1}     # conflict: mergeRounds.conflict (#90)
RUN_LINK = re.compile(r"/actions/runs/(\d+)(?:/job/(\d+))?")


def gh_checks(pr_ref: str, required: bool = False) -> list[dict]:
    """`gh pr checks --json`: exit 1 (a check failed) and 8 (checks pending) still carry the JSON;
    "no checks reported" is no checks, not an error."""
    argv = gh_argv(checkout(), ["pr", "checks", str(pr_ref), "--json", CHECK_FIELDS] + (["--required"] if required else []))
    done = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = (done.stdout or "").strip()
    if done.returncode in (0, 1, 8) and out.startswith("["):
        return [check for check in json.loads(out) if isinstance(check, dict)]
    if "no checks reported" in (done.stderr or "") or "no required checks reported" in (done.stderr or ""):
        return []
    raise RuntimeError(f"gh pr checks failed: {(done.stderr or out).strip()[:300]}")


def fetch_checks(pr_ref: str) -> dict:
    return {"all": gh_checks(pr_ref), "required": {c.get("name") for c in gh_checks(pr_ref, required=True)}}


def check_pending(check: dict) -> bool:
    return check.get("bucket") == "pending" or str(check.get("state")).upper() in ("EXPECTED", "PENDING", "QUEUED",
                                                                                   "IN_PROGRESS", "WAITING", "REQUESTED")


def split_checks(checks: dict) -> tuple[list, list, list, list]:
    """(considered, pending, failed, optional_failed). A failure counts among the required checks
    when branch protection names any, else among every check that was not skipped. Pending counts
    every check, required or not: GitHub keeps the merge state UNSTABLE until optional ones finish
    too, so the PR waits for them (r22)."""
    everything = checks.get("all") or []
    required = checks.get("required") or set()
    considered = [c for c in everything if c.get("name") in required] if required else \
        [c for c in everything if c.get("bucket") != "skipping"]
    pending = [c for c in everything if c.get("bucket") != "skipping" and check_pending(c)]
    failed = [c for c in considered if c.get("bucket") in FAILED_BUCKETS]
    optional_failed = [c for c in everything if required and c.get("name") not in required
                       and c.get("bucket") in FAILED_BUCKETS]
    return considered, pending, failed, optional_failed


def classify_ci(checks: dict) -> list[str]:
    """While anything is still running only `ci-pending:` is reported: a failure next to a running
    job is judged once the run is over (its log and a rerun need a finished run - r22)."""
    _, pending, failed, optional_failed = split_checks(checks)
    if pending:
        return [f"ci-pending: {len(pending)} check(s) still running ("
                + ", ".join(c.get("name") or "?" for c in pending) + ") - wait for the relay's ci mail (or wb.py wait-ci)"]
    lines = []
    lines += [f"ci-failed: {c.get('name')} {c.get('state')} {c.get('link') or ''}".rstrip() for c in failed]
    lines += [f"ci-optional-failed: {c.get('name')} {c.get('state')} (not a required check; the human decides)"
              for c in optional_failed]
    return lines


def check_state(pr: dict, checks: dict | None = None) -> list[str]:
    failures = []
    if pr.get("state") != "OPEN":
        failures.append(f"state: PR is {pr.get('state')}, not OPEN")
    mergeable = pr.get("mergeable")
    status = pr.get("mergeStateStatus")
    if mergeable == "CONFLICTING" or status == "DIRTY":
        failures.append(f"conflict: GitHub says {mergeable}, merge state {status} - an UPDATE round")
        return failures
    if status == "BEHIND":
        failures.append("behind: the branch is behind the base branch - an UPDATE round")
        return failures
    if mergeable != "MERGEABLE":
        retry = " (retry in ~30s)" if mergeable == "UNKNOWN" else ""
        failures.append(f"mergeable: GitHub says {mergeable}{retry}")
    if status != "CLEAN":
        ci = classify_ci(checks) if checks is not None and status in ("UNSTABLE", "BLOCKED") else []
        if ci:
            failures += ci
        else:
            retry = " (retry in ~30s)" if status == "UNKNOWN" else ""
            failures.append(f"mergeable: merge state is {status}, not CLEAN{retry}")
    return failures


def check_reviews(pr: dict) -> list[str]:
    failures = []
    if pr.get("reviewDecision") == "CHANGES_REQUESTED":
        failures.append("review: the review decision is CHANGES_REQUESTED")
    latest: dict[str, dict] = {}
    for review in sorted(pr.get("reviews") or [], key=_when):
        if PLANNER_MARKER in (review.get("body") or ""):
            continue
        if review.get("state") in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest[_login(review)] = review
    for who, review in sorted(latest.items()):
        if review.get("state") == "CHANGES_REQUESTED":
            failures.append(f"review: {who} requested changes ({_when(review)})")
    return failures


def check_labels_and_title(pr: dict) -> list[str]:
    failures = []
    for label in pr.get("labels") or []:
        name = label.get("name") if isinstance(label, dict) else str(label)
        if LABEL_HOLD_RE.search(normalize(name)):
            failures.append(f"label: the PR is labelled '{name}'")
    if HOLD_RE.search(normalize(pr.get("title") or "")):
        failures.append(f"hold: the PR title says \"{pr.get('title')}\"")
    return failures


def check_holds(pr: dict, inline: list[dict]) -> list[str]:
    """A hold word in any body the planner did not mark holds the PR, at any age. A hold is lifted
    only by its own author, later, with a comment that is nothing but "go ahead", "resume" or
    "unhold"; bots never lift. Each author's hold stands on its own."""
    bodies = []
    body = pr.get("body") or ""
    if body and PLANNER_MARKER not in body:
        bodies.append(("", _login(pr), body))      # the PR description predates every comment
    for item in list(pr.get("comments") or []) + list(pr.get("reviews") or []) + list(inline or []):
        text = item.get("body") or ""
        if text and PLANNER_MARKER not in text:
            bodies.append((_when(item), _login(item), text))
    holds: dict[str, tuple[str, str]] = {}
    for when, who, text in sorted(bodies, key=lambda entry: entry[0]):
        plain = normalize(text)
        if HOLD_RE.search(plain):
            holds[who] = (when, text)
        elif LIFT_RE.fullmatch(plain) and who in holds and not who.endswith("[bot]"):
            del holds[who]
    return [f"hold: {who} at {when or 'PR description'}: \"{' '.join(text.split())[:80]}\" "
            f"(only {who} can lift it, with a later comment that just says: go ahead)"
            for who, (when, text) in sorted(holds.items())]


def check_mail(box: str = "claude") -> list[str]:
    failures = []
    hub.reload_paths()   # AI_HUB names this checkout's mailbox
    for path in hub.unread(box):
        try:
            message = hub.parse_message(path)
        except FileNotFoundError:
            continue       # read, and so moved, after it was listed
        except (OSError, ValueError) as err:
            failures.append(f"mail: cannot read unread message {path.name}: {err}")
            continue
        if message.get("from") in ("human", "github"):
            failures.append(f"mail: unread from {message.get('from')}: {message.get('subject', '')} "
                            f"[{message.get('id', path.stem)}] - read and handle it, then check again")
    return failures


def check_relay(root: Path, number: int) -> list[str]:
    try:
        state = json.loads((root / ".workbench" / "state" / "relay.json").read_text(encoding="utf-8-sig"))
        seen = number in (state.get("seen_open") or [])
    except (OSError, ValueError, AttributeError):
        seen = False
    return [] if seen else [f"relay: the relay has not recorded PR #{number} as seen open yet - "
                            "wait for its 'PR is open' mail, then check again"]


def check_head(pr: dict, head: str) -> list[str]:
    actual = (pr.get("headRefOid") or "").lower()
    if actual != head.lower():
        return [f"head: the PR head is {actual or '?'}, not the tested {head}; run the suite on the new head"]
    return []


def check_follow_ups(root: Path) -> list[str]:
    """Autonomous (#27), or any recorded review stop, which defers minors (#64): every recorded
    follow-up is filed, and no Major+ review finding is deferred - disputed or not (r18: only
    Minor/Immaterial findings and plan items may be)."""
    stopped = any(entry["decision"] == "stop" for entry in load_review_rounds(root))
    if not (checkout_settings(root)["autonomous"] or stopped):
        return []
    failures = []
    for item in load_follow_ups(root):
        if item.get("severity") in SEVERE and str(item.get("origin", "")).startswith("review"):
            state = "ended disputed" if item.get("disputed") else "is deferred"
            failures.append(f"review: the {item['severity']} finding '{item['key']}' {state}; an autonomous "
                            "merge stops here and the human decides")
        if not item.get("url"):
            failures.append(f"follow-up: '{item['key']}' is not filed yet - run wb.py follow-up file, then check again")
    return failures


def merge_failures(pr: dict, inline: list[dict], head: str, root: Path, checks: dict | None = None) -> list[str]:
    return (check_state(pr, checks) + check_reviews(pr) + check_labels_and_title(pr) + check_holds(pr, inline) +
            check_mail() + check_relay(root, int(pr.get("number") or 0)) + check_head(pr, head) +
            check_review(root) + check_follow_ups(root))


# --- review rounds (#64) -------------------------------------------------------------------------
# After verifying a revmux report the planner records the round's decision: another round, stop (no
# verified Major: this round's fix is the last), clean, or the cap. merge-check reads the record, so
# a review that still owes a round cannot be merged, and a stop's deferred minors must be filed.
# The cap is review.maxRounds, or review.maxRoundsBig once the issue is judged big (#75).

SEVERE_SECTIONS = ("blocker", "critical", "major")
MINOR_SECTIONS = ("minor", "immaterial")
APART_SECTIONS = ("pre-existing", "open questions")      # counted, but they decide nothing
REVIEW_DECISIONS = ("continue", "stop", "clean", "cap", "limit")
# `limit` (#77): in a checkout that waits out usage limits, a round a reviewer's usage limit degraded is
# no review at all - it is rerun under the same K, so it never counts toward the cap.
REVIEW_ON_LIMIT = ("wait", "fallback")


class ReportError(ValueError):
    """A revmux report this cannot trust: exit 2, nothing recorded."""


def parse_revmux_report(text: str) -> dict:
    """Finding counts per `## ` section (`### ` headings inside it), and whether the run was degraded:
    a `## Sources` status that is not `ok` (`ok, nothing raised` is), or revmux's own DEGRADED line.
    A report without a Sources row is incomplete - revmux writes it last, so a crashed run has none."""
    counts = {name: 0 for name in SEVERE_SECTIONS + MINOR_SECTIONS + APART_SECTIONS}
    known, section, statuses, flagged = False, None, [], False
    for line in text.splitlines():
        # revmux's own line, outside any finding: a finding that quotes the phrase is not a degraded run.
        if section not in counts and re.match(r"[\s>*_]*This run is DEGRADED", line):
            flagged = True
        if line.startswith("## "):
            section = line[3:].strip().casefold()
            known = known or section in counts
            continue
        if section in counts and line.startswith("### "):
            counts[section] += 1
        elif section == "sources" and line.lstrip().startswith("|"):
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if cells[-1].casefold() == "status" or all(set(cell) <= set("-: ") for cell in cells):
                continue
            statuses.append(cells[-1])
    if not statuses:
        raise ReportError("report incomplete: no ## Sources table")
    if not known and "No findings." not in text:
        raise ReportError("report incomplete: no findings section and no 'No findings.'")
    degraded = flagged or any(not re.match(r"ok(?:,|$)", status, re.I) for status in statuses)
    return {"counts": counts, "degraded": degraded}


def review_decision(round_: int, severe: int, minor: int, degraded: bool, settings: dict) -> str:
    """settings["cap"] is the effective cap review_cap chose; without it, review.maxRounds."""
    another = "cap" if round_ >= settings.get("cap", settings.get("maxRounds", 5)) else "continue"
    if degraded:
        return another                 # a partial review is never clean, and never stops review
    if severe == 0 and minor == 0:
        return "clean"
    if severe > 0 or not settings["stopWhenNoMajor"] or round_ < settings["minRounds"]:
        return another
    return "stop"


def review_rounds_path(root: Path) -> Path:
    return root / ".workbench" / "state" / "review-rounds.json"


# --- the review cap (#75) ------------------------------------------------------------------------
# A big issue - a `Batch:` title, a `batch` or `big` label, a diff past review.bigDiffLines, or a
# checkout launched with -BigReview - gets review.maxRoundsBig rounds instead of review.maxRounds.
# Judged big once, it stays big: the verdict is latched in state/review-big.json and never removed.

BIG_LABELS = ("batch", "big")


def review_big_path(root: Path) -> Path:
    return root / ".workbench" / "state" / "review-big.json"


def issue_facts(root: Path) -> dict | None:
    """The issue's title and labels, read live, so a label added mid-loop counts. None when they cannot
    be read - no issue branch, no GitHub origin, gh failing - which is never fatal to the caller."""
    number = issue_number(root)
    if number == "?":
        return None
    try:
        done = subprocess.run(["git", "-C", str(root), "--git-dir", ".git", "remote", "get-url", "origin"],
                              capture_output=True, text=True, encoding="utf-8", errors="replace")
        if done.returncode != 0 or not triage.github_repo(done.stdout):
            return None                # workbench_repo would exit 2 here; the cap only loses a source
        done = gh_run(root, "issue", "view", number, "--json", "title,labels")
        data = json.loads(done.stdout) if done.returncode == 0 else None
    except (OSError, ValueError, SystemExit):
        return None
    if not isinstance(data, dict):
        return None
    labels = [label.get("name") for label in data.get("labels") or [] if isinstance(label, dict)]
    return {"title": str(data.get("title") or ""), "labels": [name for name in labels if isinstance(name, str)]}


def issue_md_title(root: Path) -> str | None:
    """The title the planner wrote at intake: `.workbench/issue.md`'s first `# ` line."""
    try:
        text = (root / ".workbench" / "issue.md").read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None
    return next((line[2:].strip() for line in text.splitlines() if line.startswith("# ")), None)


def diff_lines(root: Path) -> tuple[int, str] | None:
    """Lines added + deleted by the committed branch against its base (origin/HEAD's target, else
    origin/main), from the merge-base, so a merged-in default branch does not count. Binary files count
    0. None when it cannot be measured. Only root's own .git counts, as in workbench_repo."""
    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(root), "--git-dir", ".git", *args], capture_output=True,
                              text=True, encoding="utf-8", errors="replace")
    try:
        head = git("symbolic-ref", "--short", "refs/remotes/origin/HEAD")
        base = head.stdout.strip() if head.returncode == 0 and head.stdout.strip() else "origin/main"
        done = git("diff", "--numstat", f"{base}...HEAD")
    except OSError:
        return None
    if done.returncode != 0:
        return None
    total = 0
    for line in done.stdout.splitlines():
        counts = line.split("\t")[:2]
        total += sum(int(n) for n in counts if n.isdigit())
    return total, base


def judge_big(root: Path, settings: dict) -> str | None:
    """Why the issue is big now, or None: the launch flag, the title and labels, then the diff."""
    if checkout_settings(root)["bigReview"]:
        return "-BigReview"
    facts = issue_facts(root)
    title = facts["title"] if facts else issue_md_title(root)
    if title and title.strip().casefold().startswith("batch:"):
        return "title starts with Batch:" + ("" if facts else " (from issue.md; labels unknown)")
    for label in (facts or {}).get("labels", []):
        if label.strip().casefold() in BIG_LABELS:
            return f"label {label.strip()}"
    measured = diff_lines(root)
    if measured and measured[0] > settings["bigDiffLines"]:
        return f"diff {measured[0]} lines > {settings['bigDiffLines']} vs {measured[1]}"
    return None


def latched_big(root: Path) -> str | None:
    try:
        record = json.loads(review_big_path(root).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not (isinstance(record, dict) and record.get("big") is True):
        return None
    return str(record.get("reason") or "judged big earlier")


def review_cap(root: Path, settings: dict) -> dict:
    """{"cap", "big", "reason"}. A big verdict is recorded once and stays: a diff that
    shrinks later, a label removed or -NoBigReview does not lower the cap of an issue judged big."""
    reason = latched_big(root)
    if reason is None:
        reason = judge_big(root, settings)
        if reason is not None:
            from conductor import atomic_json
            atomic_json(review_big_path(root), {"big": True, "reason": reason, "at": time.time()})
    big = reason is not None
    return {"cap": settings["maxRoundsBig"] if big else settings["maxRounds"], "big": big, "reason": reason}


def recorded_cap(entry: dict) -> int:
    """The cap a round was decided under; an entry written before #75 had the fixed five."""
    cap = entry.get("cap")
    return cap if type(cap) is int and cap >= 1 else 5


def load_review_rounds(root: Path) -> list[dict]:
    try:
        entries = json.loads(review_rounds_path(root).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return []
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict) and type(entry.get("round")) is int
            and entry.get("decision") in REVIEW_DECISIONS]


def last_review_round(root: Path) -> dict | None:
    """The highest recorded round, not the last written: an earlier round may be re-recorded."""
    return max(load_review_rounds(root), key=lambda entry: entry["round"], default=None)


def check_review(root: Path) -> list[str]:
    """A revmux report with no recorded decision, or a last round that owes another round (#64).
    A checkout that ran no revmux round is not checked; a loop that was already reviewing when #64
    landed records its rounds like any other (every K can be recorded)."""
    reports = [int(match[1]) for path in (root / ".workbench" / "review").glob("revmux-r*.md")
               if (match := re.fullmatch(r"revmux-r(\d+)\.md", path.name))]
    last = last_review_round(root)
    newest = max(reports, default=0)
    if newest > (last["round"] if last else 0):
        return [f"review: revmux round {newest} has no recorded decision - run wb.py review-round --round {newest}"]
    if not last:
        return []
    k, severe = last["round"], int(last.get("severe") or 0)
    if last["decision"] == "limit":
        # The rerun's report has the same K, so the newest-report test above cannot see it (#77).
        return [f"review: round {k} hit a reviewer usage limit ({', '.join(last.get('limitedAgents') or ['?'])}); "
                f"rerun it (wb.py revmux --round {k} --rerun), then run wb.py review-round --round {k} "
                "on the rerun's report"]
    if last["decision"] == "continue":
        if last.get("degraded"):
            return [f"review: round {k} was degraded; another revmux round is due"]
        if severe:
            return [f"review: round {k} had {severe} Major finding(s); another revmux round is due"]
        return [f"review: round {k} decided continue (stopWhenNoMajor={str(last.get('stopWhenNoMajor')).lower()}, "
                f"minRounds={last.get('minRounds')}); another revmux round is due"]
    if last["decision"] == "cap" and last.get("stopWhenNoMajor") is not False:
        if last.get("degraded"):
            return [f"review: round {k} ended at the {recorded_cap(last)}-round cap (degraded) - this is the human's"]
        return [f"review: round {k} had a Major at the {recorded_cap(last)}-round cap - this is the human's"]
    return []


def save_review_rounds(root: Path, entries: list[dict]) -> None:
    from conductor import atomic_json
    atomic_json(review_rounds_path(root), entries)


def cmd_review_round(args: argparse.Namespace) -> int:
    root = checkout()
    if args.summary:
        return review_summary(root)
    if args.round is None:
        print("wb: review-round needs --round K or --summary", file=sys.stderr)
        return 2
    if args.round < 1:
        print("wb: review-round --round must be 1 or more", file=sys.stderr)
        return 2
    if args.severe is not None and args.severe < 0:
        print("wb: review-round --severe must be 0 or more", file=sys.stderr)
        return 2
    if args.reason is not None and args.severe is None:
        print("wb: review-round --reason goes with --severe", file=sys.stderr)
        return 2
    try:
        settings = followup.review_settings(followup.read_config())
    except followup.SettingsError as err:
        print(f"wb: review-round: {err}", file=sys.stderr)
        return 2
    cap = review_cap(root, settings)
    settings = {**settings, "cap": cap["cap"]}
    report = Path(args.report or f".workbench/review/revmux-r{args.round}.md")
    report = report if report.is_absolute() else root / report
    try:
        parsed = parse_revmux_report(report.read_text(encoding="utf-8-sig", errors="replace"))
    except OSError as err:
        print(f"wb: review-round: cannot read {report}: {err}", file=sys.stderr)
        return 2
    except ReportError as err:
        print(f"wb: review-round: {report.name}: {err}", file=sys.stderr)
        return 2
    counts = parsed["counts"]
    revmux_severe = sum(counts[name] for name in SEVERE_SECTIONS)
    severe = revmux_severe if args.severe is None else args.severe
    if severe < revmux_severe and not (args.reason or "").strip():
        print(f"wb: review-round: --severe {severe} is below revmux's {revmux_severe}; say why with --reason",
              file=sys.stderr)
        return 2
    # A Major verified lower is still a finding: counted as a minor, so the round stops (FIX, minRounds and
    # stopWhenNoMajor apply) instead of reading clean. One that did not reproduce at all errs the safe way.
    minor = sum(counts[name] for name in MINOR_SECTIONS) + max(revmux_severe - severe, 0)
    decision = review_decision(args.round, severe, minor, parsed["degraded"], settings)
    limited = []
    if parsed["degraded"] and checkout_settings(root)["onLimit"] == "wait":
        limited = rate_limited_agents(root, args.round)
        if limited:
            decision = "limit"
    entry = {"round": args.round, "counts": counts, "revmuxSevere": revmux_severe, "severe": severe,
             "reason": (args.reason or "").strip() or None, "degraded": parsed["degraded"], "decision": decision,
             "stopWhenNoMajor": settings["stopWhenNoMajor"], "minRounds": settings["minRounds"],
             "cap": cap["cap"], "big": cap["big"], "bigReason": cap["reason"], "at": time.time()}
    if limited:
        entry["limitedAgents"] = limited
    entries = [e for e in load_review_rounds(root) if e["round"] != args.round] + [entry]
    save_review_rounds(root, sorted(entries, key=lambda e: e["round"]))
    if decision == "limit":
        return review_limit(root, args.round, limited)
    clear_review_limit(root, args.round)
    print(f"review: {decision}{' (degraded)' if parsed['degraded'] else ''}")
    print(f"round {args.round}: {severe} Major+ (revmux {revmux_severe}), {minor} minor, "
          f"{counts['pre-existing']} pre-existing, {counts['open questions']} open question(s); "
          f"stopWhenNoMajor={'true' if settings['stopWhenNoMajor'] else 'false'} minRounds={settings['minRounds']} "
          f"cap={cap['cap']}" + (f" (big: {cap['reason']})" if cap["big"] else ""))
    return 0


# --- a Kimi-grade plan (#77) ----------------------------------------------------------------------
# When Kimi implements, the planner's plan must spell out what to edit and what not, what defines
# correct, the tests to write first, the traps and what "done" means - or say STOP AND REPORT when
# correctness rests on an outside spec or sample the repo does not have.

KIMI_PLAN_SECTIONS = ("Exact edits", "Must not change", "Tests first", "Pitfalls", "Done means")
STOP_LINE = "STOP AND REPORT"
PLAN_HEADING = re.compile(r"^\s*(?:#{1,6}\s+(?P<hash>.+?)|\*\*(?P<bold>[^*]+?)\*\*[\s:.-]*)\s*$")
PLAN_PATH = re.compile(r"(?<![\w/\\])[\w.@-]+(?:[/\\][\w.@-]+)+")
PLAN_FILE = re.compile(r"\.\w{1,8}$")                     # a last segment with a file extension
PLAN_URL = re.compile(r"\S+://\S+")
PLAN_HOST = re.compile(r"^[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}$")   # example.org: a host, not a directory
# A STOP verdict opens its line (after a list bullet, bold or backticks): a plan that restates the rule
# ("without one, the plan says STOP AND REPORT") in Pitfalls has not said STOP, and neither has a line
# that opens with the marker but states it as a condition ("STOP AND REPORT: if ...").
STOP_OPEN = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)?[*_`]*STOP AND REPORT\b")
STOP_AFTER = re.compile(r"^[\s*_`:,(\-–—]*(?:if|when|unless|whenever)\b", re.I)
STOP_CONDITION = re.compile(r"\b(?:if|when|unless|whenever|without)\b", re.I)
STOP_CLAUSE = re.compile(r"[;,:]|\s[-–—]\s")


def plan_sections(text: str) -> dict[str, list[str]]:
    """{casefolded heading: its lines} for `#`..`######` headings and lines that are only `**bold**`."""
    sections: dict[str, list[str]] = {}
    current = None
    for line in text.splitlines():
        match = PLAN_HEADING.match(line)
        if match:
            title = (match["hash"] or match["bold"]).strip().strip("*:").strip()
            title = re.sub(r"^(?:\d+[.)]\s*)", "", title)          # "1. Exact edits"
            current = title.casefold()
            sections.setdefault(current, [])
        elif current is not None:
            sections[current].append(line)
    return sections


def plan_says_stop(text: str) -> bool:
    """The plan's verdict is STOP: a line opening with the marker, or the Oracle section's first line
    carrying it ("none in the repo - STOP AND REPORT"). Either way the words right after the marker
    must not make it a condition ("STOP AND REPORT: if ..."), and on the Oracle line the clause right
    before it must not either ("if the offsets are unsourced, STOP AND REPORT"); a reason elsewhere in
    the line may use such words."""
    def conditional(after: str) -> bool:
        return bool(STOP_AFTER.match(after))

    for line in text.splitlines():
        match = STOP_OPEN.match(line)
        if match and not conditional(line[match.end():]):
            return True
    oracle = next((lines for title, lines in plan_sections(text).items() if title.startswith("oracle")), [])
    first = next((line for line in oracle if line.strip()), "")
    if STOP_LINE not in first:
        return False
    before, after = first.split(STOP_LINE, 1)
    return not STOP_CONDITION.search(STOP_CLAUSE.split(before)[-1]) and not conditional(after)


def plan_paths(line: str, root: Path | None) -> list[str]:
    """The path-looking tokens of a line that can be an oracle. With the checkout known, only what
    exists there; without it (a direct call), a last segment with a file extension. Never a URL or a
    host (an outside spec is what Kimi must not implement from, #309); `read/write`, `and/or` and
    `I/O` are prose."""
    tokens = [token for token in PLAN_PATH.findall(PLAN_URL.sub(" ", line))
              if not PLAN_HOST.match(re.split(r"[/\\]", token)[0])]
    if root is not None:
        return [token for token in tokens if (root / token).exists()]
    return [token for token in tokens if PLAN_FILE.search(token)]


def plan_problems(text: str, root: Path | None = None) -> list[str]:
    sections = plan_sections(text)

    def find(name: str) -> list[str] | None:
        key = name.casefold()
        found = [lines for title, lines in sections.items() if title.startswith(key)]
        return [line for lines in found for line in lines] if found else None

    problems = [f"missing section: {name}" for name in KIMI_PLAN_SECTIONS if find(name) is None]
    done = find("Done means")
    if done is not None and not re.search(r"\bskipped\b", "\n".join(done), re.I):
        problems.append("Done means does not ask for skipped tests by name")
    oracle = find("Oracle")
    stop = plan_says_stop(text)
    if not stop and not (oracle and any(plan_paths(line, root) for line in oracle)):
        problems.append(f"no Oracle section naming at least one path, and no {STOP_LINE} line")
    return problems


def cmd_plan_check(args: argparse.Namespace) -> int:
    root = checkout()
    tool = checkout_settings(root)["implementer"]
    if tool != "kimi":
        print(f"plan-check: not required (implementer={tool})")
        return 0
    path = Path(args.plan) if args.plan else root / ".workbench" / "plan.md"
    path = path if path.is_absolute() else root / path
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as err:
        print(f"plan-check: cannot read {path}: {err}", file=sys.stderr)
        return 2
    problems = plan_problems(text, root)
    for problem in problems:
        print(f"plan-check: {problem}")
    if problems:
        print(f"plan-check: {path.name} is not a Kimi-grade plan (start-github-issue.md, Phase 2)")
        return 1
    print("plan-check: ok" + (f" ({STOP_LINE})" if plan_says_stop(text) else ""))
    return 0


# --- a reviewer's usage limit in a checkout that waits limits out (#77) ------------------------------

def review_limit_path(root: Path) -> Path:
    return root / ".workbench" / "state" / "review-limit.json"


def run_record_path(root: Path, round_: int) -> Path:
    return root / ".workbench" / "review" / f"revmux-r{round_}.json"


def read_run_record(root: Path, round_: int) -> dict | None:
    """run-revmux.ps1's record of the revmux run behind revmux-r<K>.md: {run, dir, profile, attempt, scope}."""
    try:
        record = json.loads(run_record_path(root, round_).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def rate_limited_agents(root: Path, round_: int) -> list[str]:
    """The agents revmux degraded for a rate limit in round K's run, from its events.jsonl: the
    `agent_degraded` events whose text says `rate limited` (revmux find.go's fault, every executor).
    The markdown Sources table only says `degraded`. Empty when the run cannot be read."""
    record = read_run_record(root, round_)
    if not record or not isinstance(record.get("dir"), str) or not record["dir"]:
        return []
    try:
        lines = (Path(record["dir"]) / "events.jsonl").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    agents = []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if (isinstance(event, dict) and event.get("kind") == "agent_degraded"
                and "rate limited" in str(event.get("text") or "")):
            agent = str(event.get("agent") or "?")
            if agent not in agents:
                agents.append(agent)
    return agents


def limited_tool(root: Path, round_: int, agents: list[str]) -> str:
    """The tool behind the limited agents, for the queue's status line: kimi, codex or claude. An
    agent is a lens group (`bugs+impl`), so its executor comes from the run's manifest.json; a guess
    from the name only for what the manifest does not list (synthesis, verify) or when it is unreadable."""
    executors: dict[str, str] = {}
    record = read_run_record(root, round_)
    if record and isinstance(record.get("dir"), str) and record["dir"]:
        try:
            manifest = json.loads((Path(record["dir"]) / "manifest.json").read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            manifest = None
        listed = manifest.get("agents") if isinstance(manifest, dict) else None
        for agent in listed if isinstance(listed, list) else []:
            if isinstance(agent, dict) and isinstance(agent.get("name"), str) and isinstance(agent.get("executor"), str):
                executors[agent["name"]] = agent["executor"]
    tools = [executors[agent] for agent in agents if agent in executors]
    for tool in ("kimi", "codex", "claude"):
        if tool in tools:
            return tool
    for tool in ("kimi", "codex", "claude"):
        if any(tool in agent.casefold() for agent in agents if agent not in executors):
            return tool
    return tools[0] if tools else agents[0] if agents else "?"


def review_on_limit() -> str:
    """`reviewOnLimit` from ~/.agworkbench.json: wait (default) or fallback. The launcher refuses others."""
    try:
        value = followup.read_config().get("reviewOnLimit")
    except followup.SettingsError:
        return "wait"
    return value if value in REVIEW_ON_LIMIT else "wait"


def limit_retry_minutes() -> float:
    import relay
    return relay.limit_retry_setting()


def review_limit(root: Path, round_: int, agents: list[str]) -> int:
    print(f"review: limit (reviewer usage limit: {', '.join(agents)})")
    print(f"round {round_} is no review: it does not count toward the cap, and merge-check refuses it until "
          f"it is rerun and recorded")
    if review_on_limit() == "fallback":
        clear_review_limit(root, round_)
        print(f"next: rerun now with python \"$AGWORKBENCH/lib/wb.py\" revmux --round {round_} --rerun "
              "--profile claude-only (reviewOnLimit=fallback)")
        return 0
    minutes = max(1, math.ceil(limit_retry_minutes()))      # revmux --after takes whole minutes
    since = time.time()
    from conductor import atomic_json
    atomic_json(review_limit_path(root), {"tool": limited_tool(root, round_, agents), "agents": agents, "since": since,
                                          "retryAt": since + minutes * 60, "round": round_})
    print(f"next: rerun with python \"$AGWORKBENCH/lib/wb.py\" revmux --round {round_} --rerun --after {minutes} "
          "(it waits on screen, then reviews); keep your waiter and end your turn")
    return 0


def clear_review_limit(root: Path, round_: int) -> None:
    """A recorded decision other than `limit` for round K ends a wait on round K or an earlier one."""
    path = review_limit_path(root)
    try:
        record = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return
    except (OSError, ValueError):
        record = None
    waited = record.get("round") if isinstance(record, dict) else None
    if type(waited) is not int or waited <= round_:
        path.unlink(missing_ok=True)


def review_summary(root: Path) -> int:
    """The line for the PR body and the merge note: why review ended, and where every minor deferred
    by a stop went - the URLs once filed, "not filed yet" before."""
    entries = load_review_rounds(root)
    last = max(entries, key=lambda entry: entry["round"], default=None)
    if not last:
        print("wb: review-round --summary: no review round recorded", file=sys.stderr)
        return 2
    k, decision = last["round"], last["decision"]
    if decision == "limit":
        print(f"review: round {k} hit a reviewer usage limit; rerun it first - no summary yet")
        return 1
    if decision not in ("stop", "clean") and not (decision == "cap" and last.get("stopWhenNoMajor") is False):
        print(f"review: round {k} decided {decision}; no summary yet")
        return 1
    stops = {f"r{entry['round']}": entry["round"] for entry in entries if entry["decision"] == "stop"}
    items = [item for item in load_follow_ups(root) if followup.round_of(item.get("origin")) in stops]
    deferred = ""
    if items:
        rounds = sorted({stops[followup.round_of(item.get("origin"))] for item in items})
        origin = "" if rounds == [k] else f" from round {', '.join(map(str, rounds))}"
        if all(item.get("url") for item in items):
            deferred = f"{len(items)} minor finding(s){origin} in {', '.join(dict.fromkeys(item['url'] for item in items))}"
        else:
            deferred = f"{len(items)} minor finding(s){origin} deferred as follow-ups (not filed yet)"
    if decision == "stop":
        line = f"review stopped: round {k} had no Major; {deferred or 'all findings fixed'}"
    else:
        line = f"review clean after round {k}" if decision == "clean" else f"review ended at the {recorded_cap(last)}-round cap (round {k})"
        line += f"; {deferred}" if deferred else ""
    revmux_severe, severe = int(last.get("revmuxSevere") or 0), int(last.get("severe") or 0)
    if severe < revmux_severe:
        line += f" (revmux {revmux_severe} Major+, verified {severe}: {last.get('reason')})"
    print(line)
    return 0


# --- follow-up issues (#27) ----------------------------------------------------------------------
# One machine-readable list the planner fills as it defers findings or agrees a plan's out-of-scope
# items; `follow-up file` turns each unfiled item into its own issue or a line of the PR's shared
# leftovers issue, and writes the URL back.

SEVERITIES = ("blocker", "major", "minor", "immaterial", "plan")
SEVERE = ("blocker", "major")
FOLLOW_UP_LABEL = "follow-up"
NESTED_LABEL = "follow-up-nested"


def follow_ups_path(root: Path) -> Path:
    return root / ".workbench" / "state" / "follow-ups.json"


def load_follow_ups(root: Path) -> list[dict]:
    try:
        items = json.loads(follow_ups_path(root).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return []
    return [item for item in items if isinstance(item, dict) and item.get("key")] if isinstance(items, list) else []


def save_follow_ups(root: Path, items: list[dict]) -> None:
    from conductor import atomic_json
    atomic_json(follow_ups_path(root), items)


def cmd_follow_up_add(args: argparse.Namespace) -> int:
    root = checkout()
    body = Path(args.body_file).read_text(encoding="utf-8-sig") if args.body_file else ""
    items = load_follow_ups(root)
    item = next((existing for existing in items if existing["key"] == args.key), None)
    if item is None:
        item = {"key": args.key}
        items.append(item)
    if item.get("url"):
        # Filed already: the issue stays, but merge-check gates on severity and disputed (r18 m8).
        if item.get("leftovers") and (item.get("severity") != args.severity or item.get("origin") != args.origin):
            item["refresh"] = True
        item.update(severity=args.severity, origin=args.origin, disputed=bool(args.disputed))
        save_follow_ups(root, items)
        print(f"{args.key}: already filed as {item['url']}; severity/origin/disputed updated")
        return 0
    item.update(title=args.title, body=body, severity=args.severity, origin=args.origin,
                disputed=bool(args.disputed), ownIssue=bool(args.own_issue))
    if args.file:
        item["file"] = args.file              # #42: named in a duplicate report, a hint to the matcher
    save_follow_ups(root, items)
    print(f"{args.key}: recorded ({args.severity}{', disputed' if args.disputed else ''})")
    return 0


# --- the workbench's own repo (#71) ----------------------------------------------------------------
# In a fork's checkout gh resolves its base repository to the fork's parent, so a gh call without
# --repo reads and writes upstream. Every repo-scoped call names this checkout's origin instead, and
# a write aimed at another repo is refused before gh runs.

class ForeignRepo(RuntimeError):
    """A gh write aimed at a repo other than the workbench's own."""


REPO_WRITES = {
    "issue": {"create", "edit", "comment", "close", "reopen", "delete", "transfer", "lock", "unlock",
              "pin", "unpin", "develop"},
    "pr": {"create", "edit", "comment", "merge", "close", "reopen", "review", "ready", "lock", "unlock"},
    "label": {"create", "edit", "delete", "clone"},
    "run": {"rerun", "cancel", "delete"},
}
URL_REPO = re.compile(r"https?://github\.com/([^/\s]+/[^/\s]+)/(?:issues|pull|actions/runs)/\d+\S*", re.I)
# The value-taking flags of the issue/pr/label/run commands: their value is never read as a flag (r1 M1).
# One set for every command, so a short letter can be misread (`-s` is a value for `issue list`, a
# switch for `pr merge`). That only decides where `--repo` goes: a write checks every token for another
# repo's URL (r2 m1), so a misread can never let one through.
VALUE_FLAGS = {"-R", "--repo", "-t", "--title", "-b", "--body", "-F", "--body-file", "-S", "--search", "-l", "--label",
               "--add-label", "--remove-label", "-H", "--head", "-B", "--base", "-s", "--state", "--json", "-q", "--jq",
               "-T", "--template", "-L", "--limit", "-c", "--color", "--comment", "-d", "--description", "-j", "--job",
               "--match-head-commit", "-r", "--reason", "-a", "--assignee", "--add-assignee", "--remove-assignee",
               "-m", "--milestone", "-A", "--author", "-p", "--project", "--add-project", "--remove-project",
               "--subject", "--app", "--mention", "--duplicate-of", "-w", "--workflow", "--branch", "--event",
               "-u", "--user", "--commit", "--status", "--attempt", "-n", "--name", "-i", "--interval",
               "--reviewer", "--add-reviewer", "--remove-reviewer", "--required-checks"}
API_VALUE_FLAGS = {"-X", "--method", "-f", "--raw-field", "-F", "--field", "-H", "--header", "--input",
                   "-q", "--jq", "-t", "--template", "--hostname", "--cache", "-p", "--preview"}
API_FIELD_FLAGS = ("-f", "--raw-field", "-F", "--field", "--input")


def workbench_repo(root: Path) -> str:
    """`owner/name` of the checkout's origin, case kept. Only root's own .git counts: a folder inside
    another clone is not a checkout (r1 critique 1). No origin on GitHub is exit 2, never gh's guess."""
    done = subprocess.run(["git", "-C", str(root), "--git-dir", ".git", "remote", "get-url", "origin"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    repo = triage.github_repo(done.stdout) if done.returncode == 0 else None
    if not repo:
        print(f"wb: cannot tell this workbench's repository: {root} has no GitHub origin "
              f"({(done.stderr or done.stdout or '').strip() or 'not a clone'}); gh is not left to guess (#71)",
              file=sys.stderr)
        raise SystemExit(2)
    return repo


def same_repo(a: str, b: str) -> bool:
    return a.casefold() == b.casefold()


def repo_value(value: str) -> str:
    """The OWNER/REPO of a `--repo` value, which gh also accepts as HOST/OWNER/REPO."""
    parts = value.strip().strip("/").split("/")
    return "/".join(parts[-2:]) if len(parts) == 3 else value.strip()


def split_args(args: list[str]) -> tuple[str | None, int | None]:
    """(the `--repo` value, the index of the `--` that ends the flags) of an issue/pr/label/run call,
    read the way gh reads it: a flag's value - a title like `-Recurse` or `--` - is never a flag (r1 M1,
    r2 i1). An unknown flag is taken for a switch."""
    repo, i = None, 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            return repo, i
        if not arg.startswith("-") or arg == "-":
            i += 1
            continue
        long = arg.startswith("--")
        name = arg.split("=", 1)[0] if long else arg[:2]
        joined = "=" in arg if long else len(arg) > 2        # --title=x, -tx: the value is in this token
        takes = name in VALUE_FLAGS
        if name in ("--repo", "-R"):
            value = (arg[len(name) + 1:] if long else arg[2:]) if joined else (args[i + 1] if i + 1 < len(args) else "")
            repo = repo_value(value)
        i += 2 if takes and not joined else 1
    return repo, None


def refuse(what: str, target: str, repo: str) -> ForeignRepo:
    return ForeignRepo(f"refused: gh {what} targets {target}, not this workbench's repo {repo} (#71)")


def api_flag(arg: str) -> str | None:
    """The value-taking `gh api` flag arg starts, spelled `-X`, `-XPOST`, `--method` or `--method=POST`."""
    for flag in API_VALUE_FLAGS:
        if arg == flag or arg.startswith(flag + "=") or (not flag.startswith("--") and arg.startswith(flag)):
            return flag
    return None


def scoped_api(args: list[str], repo: str) -> list[str]:
    owner, name = repo.split("/")
    # gh fills {owner}/{repo} from its own base resolution - the #71 bug - so they are filled here.
    fill = lambda text: text.replace("{owner}", owner).replace("{repo}", name)
    out, method, fields, endpoint, i = list(args), None, False, None, 1
    while i < len(out):
        flag = api_flag(out[i])
        if flag is None:
            if endpoint is None and not out[i].startswith("-"):
                out[i] = endpoint = fill(out[i])
            i += 1
            continue
        at = i if out[i] != flag else i + 1           # the token that holds the value
        value = (out[at][len(flag):].lstrip("=") if at == i else out[at]) if at < len(out) else ""
        if flag in ("-X", "--method"):
            method = value.upper()
        elif flag in API_FIELD_FLAGS:
            fields = True
            if flag in ("-F", "--field"):
                out[at] = fill(out[at])
        i = at + 1
    writes = method != "GET" if method else fields
    target = re.match(r"/?repos/([^/?#]+/[^/?#]+)", endpoint or "")
    if writes and target and not same_repo(target[1], repo):
        raise refuse(f"api {endpoint}", target[1], repo)
    return out


def scoped(args: list[str] | tuple[str, ...], repo: str) -> list[str]:
    """args with the workbench repo named; ForeignRepo for a write to another repo. `api graphql` is
    not inspected: the workbench sends no mutations."""
    args = list(args)
    if args[:1] == ["api"]:
        return scoped_api(args, repo)
    if args[:1] and args[0] in REPO_WRITES:
        named, end = split_args(args[2:])
        if args[1:2] and args[1] in REPO_WRITES[args[0]]:
            # Fail closed (r2 m1): any token naming another repo refuses a write, whatever reads it.
            urls = [match[1] for match in map(URL_REPO.fullmatch, args[2:]) if match]
            target = next((t for t in [named, *urls] if t and not same_repo(t, repo)), None)
            if target:
                raise refuse(' '.join(args[:2]), target, repo)
        # --repo whenever no flag names one: a URL argument wins over it in gh, and a value we misread
        # can then never leave the repo to gh's base (r1 M1). Before a `--` that ends the flags.
        if named is None:
            at = len(args) if end is None else end + 2
            return args[:at] + ["--repo", repo] + args[at:]
    return args


def gh_argv(root: Path, args) -> list[str]:
    return ["gh", *scoped(args, workbench_repo(root))]


def gh_run(root: Path, *args: str) -> subprocess.CompletedProcess:
    """A refusal is a failed call (exit 1), so every caller's failure path reports it."""
    try:
        argv = gh_argv(root, args)
    except ForeignRepo as err:
        print(f"wb: {err}", file=sys.stderr)
        return subprocess.CompletedProcess(["gh", *args], 1, "", str(err))
    return subprocess.run(argv, cwd=str(root), capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def issue_body(item: dict, source: int, pr: int | None, related: list[str] = (), marker: str = "") -> str:
    where = f"Source: #{source}" + (f", PR #{pr}" if pr else "")
    notes = "".join(f"{line}\n" for line in related) + ("\n" if related else "")
    return (f"{item.get('body', '').rstrip()}\n\n{notes}{where}\n"
            f"Severity: {item.get('severity')}; origin: {item.get('origin')}"
            f"{'; disputed' if item.get('disputed') else ''}\n\n"
            f"<!-- agworkbench:follow-up source=#{source} -->\n"
            + (f"{marker}\n" if marker else "") + f"{PLANNER_MARKER}\n")


def cmd_follow_up_file(args: argparse.Namespace) -> int:
    root = checkout()
    items = load_follow_ups(root)
    pending = [item for item in items if not item.get("url")]
    refreshing = [item for item in items if item.get("leftovers") and item.get("refresh")]
    if not pending and not refreshing:
        print("no unfiled follow-ups")
        return 0
    try:
        config = followup.read_config()
        settings = followup.load_settings(config)
    except followup.SettingsError as err:
        print(f"wb: follow-up: {err}", file=sys.stderr)
        return 2
    if settings["dedupe"] and not args.pr:
        print("wb: follow-up file needs --pr while followUp.dedupe is on (the duplicate reports name the PR)",
              file=sys.stderr)
        return 2
    # One level of chaining at most: a follow-up of a follow-up is filed under another label.
    source = gh_run(root, "issue", "view", str(args.source), "--json", "title,labels")
    if source.returncode != 0:
        print(f"wb: follow-up: cannot read issue #{args.source}: {source.stderr.strip()}", file=sys.stderr)
        return 1
    source_info = json.loads(source.stdout)
    labels = {label.get("name") for label in source_info.get("labels", [])}
    label = NESTED_LABEL if labels & {FOLLOW_UP_LABEL, NESTED_LABEL} else FOLLOW_UP_LABEL
    created = gh_run(root, "label", "create", label, "--color", "BFD4F2",
                     "--description", "filed automatically by an agworkbench loop")
    use_label = created.returncode == 0 or "already exists" in (created.stderr or "")
    if not use_label:
        print(f"wb: follow-up: cannot create label '{label}' ({created.stderr.strip()}); filing without it")
    if settings["dedupe"]:
        return file_deduped(root, args, items, pending, label if use_label else None, config, settings,
                            source_info.get("title") or f"Issue #{args.source}")
    if args.pr:
        return file_without_dedupe(root, args, items, pending, label if use_label else None, config,
                                   source_info.get("title") or f"Issue #{args.source}")
    return file_legacy(root, args, items, pending, label if use_label else None)


def file_legacy(root: Path, args: argparse.Namespace, items: list[dict], pending: list[dict],
                label: str | None) -> int:
    failed = 0
    for item in pending:
        # A quoted phrase: `-Flag`, `word:`, `#12` or a quote in a title are not search syntax (r18 m9).
        phrase = '"' + item["title"].replace('"', " ").strip() + '"'
        found = gh_run(root, "issue", "list", "--state", "open", "--search", f"{phrase} in:title",
                       "--json", "title,url", "--limit", "200")
        if found.returncode != 0:
            print(f"wb: follow-up: search failed for '{item['key']}': {found.stderr.strip()}", file=sys.stderr)
            failed += 1
            continue
        same = [issue for issue in json.loads(found.stdout or "[]") if issue.get("title") == item["title"]]
        if same:
            item["url"] = same[0]["url"]
            print(f"{item['key']}: already open as {item['url']}")
        else:
            body_file = root / ".workbench" / "state" / f"follow-up-{item['key']}.md"
            body_file.write_text(issue_body(item, args.source, args.pr), encoding="utf-8")
            argv = ["issue", "create", "--title", item["title"], "--body-file", str(body_file)]
            if label:
                argv += ["--label", label]
            done = gh_run(root, *argv)
            body_file.unlink(missing_ok=True)
            url = next((line.strip() for line in (done.stdout or "").splitlines() if "/issues/" in line), None)
            if done.returncode != 0 or not url:
                print(f"wb: follow-up: filing '{item['key']}' failed: {(done.stderr or done.stdout).strip()}",
                      file=sys.stderr)
                failed += 1
                continue
            item["url"] = url
            print(f"{item['key']}: filed {url}")
        save_follow_ups(root, items)          # after each one, so a failure never loses a filed url
    return 1 if failed else 0


def file_without_dedupe(root: Path, args: argparse.Namespace, items: list[dict], pending: list[dict],
                        label: str | None, config: dict, source_title: str) -> int:
    own = [i for i in pending if followup.own_issue(i)]
    failed = file_legacy(root, args, items, own, label)
    if any(not followup.own_issue(i) for i in pending) or any(i.get("refresh") for i in items):
        try:
            repo = workbench_repo(root)
            file_leftovers(root, repo, args, items, [], label, source_title,
                           triaged=followup.triage_on(config, repo))
        except (DedupeFailed, ValueError, KeyError, TypeError) as err:
            print(f"wb: follow-up: leftovers: {err}", file=sys.stderr)
            failed = 1
    return failed


# --- dedupe and bump (#42) -----------------------------------------------------------------------
# With followUp.dedupe on (the default), a finding that an existing issue already describes becomes a
# duplicate report on it, and the reports bump its priority. lib/followup.py holds the rules.

PRIORITY_LABEL = re.compile(r"priority:P[0-3]", re.I)


class DedupeFailed(Exception):
    """This item only: nothing more is written for it, the run goes on."""


def gh_ok(root: Path, what: str, *args: str) -> subprocess.CompletedProcess:
    done = gh_run(root, *args)
    if done.returncode != 0:
        raise DedupeFailed(f"{what}: {(done.stderr or done.stdout).strip()}")
    return done


def fetch_issues(root: Path, repo: str, label: str, state: str) -> list[dict]:
    done = gh_ok(root, f"listing '{label}' issues", "api", "--paginate",
                 f"repos/{repo}/issues?labels={quote(label, safe='')}&state={state}&per_page=100")
    return [c for c in map(followup.candidate, followup.parse_pages(done.stdout)) if c]


def ensure_label(root: Path, name: str) -> bool:
    created = gh_run(root, "label", "create", name, "--color", "D93F0B",
                     "--description", "agworkbench priority (#34, #42)")
    return created.returncode == 0 or "already exists" in (created.stderr or "")


def dup_comment(item: dict, args: argparse.Namespace, count: int, bump: str) -> str:
    round_ = followup.round_of(item.get("origin"))
    where = f" in `{item['file']}`" if item.get("file") else ""
    lines = [f"Reported again: PR #{args.pr} ({'plan item' if round_ == 'plan' else 'review ' + round_}) "
             f"of #{args.source}{where}.", "", f"**{item.get('title', '')}**", ""]
    if (item.get("body") or "").strip():
        lines += [item["body"].rstrip(), ""]
    if bump:
        lines += [bump, ""]
    lines += [f"Duplicate reports so far: {count} ({count + 1} reports in all).", "",
              followup.dup_marker(item, args.source, args.pr), f"<!-- agworkbench:dup-count {count} -->",
              PLANNER_MARKER]
    return "\n".join(lines) + "\n"


OWN = "own"      # record_duplicate's message when the match is the item's own issue (r1 M1)


def read_issue(root: Path, repo: str, number: int) -> dict:
    issue = followup.candidate(json.loads(gh_ok(root, f"reading #{number}", "api", f"repos/{repo}/issues/{number}").stdout))
    if issue is None:
        raise DedupeFailed(f"#{number} is not an issue")
    return issue


def find_own_unlabelled(root: Path, repo: str, item: dict, args: argparse.Namespace) -> dict | None:
    """Without the follow-up label, an issue an earlier run filed for this item is no candidate; the
    #27 exact-title search finds it, and its trusted finding marker proves it is this item's (r1 M1)."""
    return find_by_title(root, repo, item["title"], f"searching for '{item['key']}'",
                         lambda issue: followup.is_own(issue, item, args.source, args.pr))


def find_by_title(root: Path, repo: str, title: str, what: str, accept) -> dict | None:
    phrase = '"' + title.replace('"', " ").strip() + '"'
    found = gh_ok(root, what, "issue", "list", "--state", "open", "--search",
                  f"{phrase} in:title", "--json", "number,title", "--limit", "200")
    for hit in json.loads(found.stdout or "[]"):
        if hit.get("title") == title and isinstance(hit.get("number"), int):
            issue = read_issue(root, repo, hit["number"])
            if accept(issue):
                return issue
    return None


def find_leftovers(root: Path, repo: str, args: argparse.Namespace, items: list[dict],
                   pool: list[dict], title: str) -> dict | None:
    accept = lambda issue: (followup.closed_reason(issue) is None
                            and followup.own_leftovers(issue, args.source, args.pr))
    prior = next((i for i in items if i.get("leftovers") and i.get("url")), None)
    if prior:
        match = re.search(r"/issues/(\d+)$", prior["url"])
        if match:
            issue = read_issue(root, repo, int(match[1]))
            if accept(issue):
                return issue
    own = next((c for c in pool if accept(c)), None)
    if own:
        return own
    return find_by_title(root, repo, title, "searching for leftovers", accept)


def file_leftovers(root: Path, repo: str, args: argparse.Namespace, items: list[dict],
                   pool: list[dict], label: str | None, source_title: str, *, triaged: bool,
                   excluded: set[str] = frozenset()) -> None:
    listed = [i for i in items if (i.get("leftovers") or (not i.get("url") and not followup.own_issue(i)))
              and not i.get("duplicateOf")
              and i["key"] not in excluded]
    if not listed or not any(not i.get("url") or i.get("refresh") for i in listed):
        return
    title = f"Leftovers from #{args.source}: {source_title}"
    existing = find_leftovers(root, repo, args, items, pool, title)
    active = [i for i in listed if not i.get("url") or (existing and i.get("url") == existing["url"])]
    orphaned = [i for i in listed if i.get("refresh") and i not in active]
    for item in orphaned:
        number = (re.search(r"/issues/(\d+)$", item["url"] or "") or [None, "?"])[1]
        print(f"wb: follow-up: leftovers #{number} is closed or no longer recognised; "
              f"re-rating of '{item['key']}' to {item.get('severity')} not written there - update it by hand")
        item.pop("refresh", None)
    if orphaned:
        save_follow_ups(root, items)
    if not active or not any(not i.get("url") or i.get("refresh") for i in active):
        return
    old_body = existing["body"] if existing else ""
    severity = followup.leftovers_severity(active)
    target = followup.severity_priority(severity)
    body = (followup.extend_leftovers_body(old_body, active, severity) if existing else
            followup.leftovers_body(active, args.source, args.pr))
    body_file = root / ".workbench" / "state" / "follow-up-leftovers.md"
    body_file.write_text(body, encoding="utf-8")
    try:
        if existing:
            if body != old_body:
                gh_ok(root, f"editing leftovers #{existing['number']}", "issue", "edit",
                      str(existing["number"]), "--body-file", str(body_file))
            url = existing["url"]
            current = triage.priority_of(existing["labels"])
            if not triaged and (current is None or triage.RANK[target] < triage.RANK[current]):
                try:
                    set_priority(root, existing["number"], existing["labels"], target,
                                 f"labelling leftovers #{existing['number']}")
                except DedupeFailed as err:
                    print(f"wb: follow-up: {err}; leftovers #{existing['number']} keeps "
                          f"{f'priority:{current}' if current else 'no priority label'}")
        else:
            argv = ["issue", "create", "--title", title, "--body-file", str(body_file)]
            names = [label] if label else []
            if not triaged:
                priority = f"priority:{target}"
                if ensure_label(root, priority):
                    names.append(priority)
                else:
                    print(f"wb: follow-up: cannot create label '{priority}'; filing leftovers untriaged")
            for name in names:
                argv += ["--label", name]
            done = gh_ok(root, "creating leftovers", *argv)
            url = next((line.strip() for line in (done.stdout or "").splitlines() if "/issues/" in line), None)
            if not url:
                raise DedupeFailed("creating leftovers returned no issue URL")
        for item in active:
            if not item.get("url"):
                item.update(url=url, leftovers=True)
            item.pop("refresh", None)
        save_follow_ups(root, items)
        print(f"{len(active)} leftover(s): filed {url}")
    finally:
        body_file.unlink(missing_ok=True)


def set_priority(root: Path, number: int, labels: list[str], target: str, what: str) -> None:
    """Raise an issue's priority label, replacing its old priority label if present."""
    name = f"priority:{target}"
    if not ensure_label(root, name):
        raise DedupeFailed(f"cannot create label '{name}'")
    argv = ["issue", "edit", str(number), "--add-label", name]
    for old in labels:
        if PRIORITY_LABEL.fullmatch(old.strip()) and old != name:
            argv += ["--remove-label", old]
    gh_ok(root, what, *argv)


def record_duplicate(root: Path, repo: str, item: dict, number: int, semantic: bool,
                     args: argparse.Namespace, bump_at: dict) -> tuple[dict | None, str]:
    """Comment the duplicate on #number (reopening it when closed as completed) and bump its label.
    (issue, message) when recorded; (issue, OWN) when #number is the item's own issue, filed by a run
    that died before saving its url; (None, related line) when it must be filed new after all."""
    issue = read_issue(root, repo, number)
    if followup.is_own(issue, item, args.source, args.pr):
        return issue, OWN
    comments = followup.parse_pages(gh_ok(root, f"reading #{number}'s comments", "api", "--paginate",
                                          f"repos/{repo}/issues/{number}/comments?per_page=100").stdout)
    state = followup.dup_state(issue, [c for c in comments if isinstance(c, dict)])
    already = (str(args.source), str(args.pr), item["key"]) in state["idents"]
    reason = followup.closed_reason(issue)
    if reason and not already:
        if semantic or reason != "completed":
            return None, f"Possibly related: #{number} (closed as {reason})"
        gh_ok(root, f"reopening #{number}", "issue", "reopen", str(number))
    count = state["count"] + (0 if already else 1)
    current = triage.priority_of(issue["labels"])
    target = followup.target_priority(current, count + 1, bump_at)
    bump = ""
    if target != current:
        this = followup.report_label(dict(pr=args.pr, round=followup.round_of(item.get("origin"))))
        bump = (f"priority {current or 'untriaged'} -> {target}: reported {count + 1} times "
                f"({', '.join(state['reports'] + ([] if already else [this]))})")
    if not already:
        body_file = root / ".workbench" / "state" / f"follow-up-{item['key']}-dup.md"
        body_file.write_text(dup_comment(item, args, count, bump), encoding="utf-8")
        try:
            gh_ok(root, f"commenting on #{number}", "issue", "comment", str(number), "--body-file", str(body_file))
        finally:
            body_file.unlink(missing_ok=True)
    if target != current:
        # After the comment: a crash in between heals on the rerun, which finds its own marker.
        set_priority(root, number, issue["labels"], target, f"labelling #{number}")
    shown = f"{current or 'untriaged'} -> {target}" if target != current else (current or "untriaged")
    return issue, (f"duplicate of #{number} ({'semantic' if semantic else 'exact'}"
                   f"{', already recorded' if already else ''}); {count} duplicate(s), priority {shown}")


def file_deduped(root: Path, args: argparse.Namespace, items: list[dict], pending: list[dict],
                 label: str | None, config: dict, settings: dict, source_title: str) -> int:
    note = lambda text: print(f"wb: follow-up: {text}")
    if label is None:
        note("an issue filed without the follow-up label cannot be found as a duplicate later")
    try:
        repo = workbench_repo(root)
        candidates = {}
        for name in (FOLLOW_UP_LABEL, NESTED_LABEL):
            for issue in fetch_issues(root, repo, name, "all"):
                candidates.setdefault(issue["number"], issue)
    except (DedupeFailed, ValueError, KeyError, TypeError) as err:
        print(f"wb: follow-up: {err}", file=sys.stderr)
        return 1
    candidates.pop(args.source, None)                     # the issue this PR closes cannot hold its findings
    pool = [c for c in candidates.values() if not followup.own_leftovers(c, args.source, args.pr)]
    labelled = {FOLLOW_UP_LABEL, NESTED_LABEL}
    rest = [item for item in pending if followup.pick_exact(item, pool, labelled)[0] is None]
    semantic, semantic_pool = {}, {}
    if rest:
        bugs = []
        bug = triage.bug_label_of(config)
        if bug is None:
            note("bugLabel is invalid; bug issues are not semantic candidates")
        else:
            try:
                bugs = fetch_issues(root, repo, bug, "open")
            except DedupeFailed as err:
                note(f"{err}; bug issues are not semantic candidates")
        semantic_pool = {c["number"]: c for c in pool + bugs if c["state"] == "open"
                         and c["number"] != args.source
                         and not followup.own_leftovers(c, args.source, args.pr)}
        semantic = followup.semantic_matches(rest, list(semantic_pool.values()), note)
    triaged = followup.triage_on(config, repo)
    failed = 0
    excluded = set()
    for item in pending:
        try:
            own = next((c for c in pool if followup.is_own(c, item, args.source, args.pr)), None)
            if own is None and followup.pick_exact(item, pool, labelled)[0] is None:
                # The crashed run may have filed without the label even if this run has it (r2 m1).
                own = find_own_unlabelled(root, repo, item, args)
            if own is not None:
                adopt(item, own)
                save_follow_ups(root, items)
                continue
            match, related = followup.pick_exact(item, pool, labelled)   # again: issues filed in this run count
            notes = [f"Possibly related: #{related['number']} (closed as {followup.closed_reason(related)})"] if related else []
            number, is_semantic = (match["number"], False) if match else (None, False)
            if number is None and item["key"] in semantic:
                guess, confidence = semantic[item["key"]]
                trusted = followup.trusted_author(semantic_pool.get(guess, {}).get("author_association"))
                if guess is not None and confidence == "high" and trusted:
                    number, is_semantic = guess, True
                elif guess is not None:
                    outside = "" if trusted or confidence != "high" else ", opened outside the repo's collaborators"   # r1 m3
                    notes.append(f"Possibly related: #{guess} ({confidence} confidence{outside})")
            if number is not None:
                issue, message = record_duplicate(root, repo, item, number, is_semantic, args, settings["bumpAt"])
                if message == OWN:
                    adopt(item, issue)
                    save_follow_ups(root, items)
                    continue
                if issue is not None:
                    item.update(url=issue["url"], duplicateOf=number)
                    print(f"{item['key']}: {message}")
                    save_follow_ups(root, items)
                    continue
                notes.append(message)
            if not followup.own_issue(item):
                if notes:
                    item["related"] = notes
                continue
            filed = file_new(root, item, args, label, notes, triaged)
            pool.append(filed)
            item["url"] = filed["url"]
            print(f"{item['key']}: filed {filed['url']}")
        except (DedupeFailed, ValueError, KeyError, TypeError) as err:
            print(f"wb: follow-up: '{item['key']}': {err}", file=sys.stderr)
            failed += 1
            excluded.add(item["key"])
            continue
        save_follow_ups(root, items)          # after each one, so a failure never loses a filed url
    if (any(not followup.own_issue(i) and not i.get("url") for i in pending)
            or any(i.get("leftovers") and i.get("refresh") for i in items)):
        try:
            file_leftovers(root, repo, args, items, list(candidates.values()), label, source_title,
                           triaged=triaged, excluded=excluded)
        except (DedupeFailed, ValueError, KeyError, TypeError) as err:
            print(f"wb: follow-up: leftovers: {err}", file=sys.stderr)
            failed += 1
    return 1 if failed else 0


def adopt(item: dict, issue: dict) -> None:
    item["url"] = issue["url"]
    item.pop("duplicateOf", None)
    print(f"{item['key']}: already filed as {issue['url']} (by an earlier run)")


def file_new(root: Path, item: dict, args: argparse.Namespace, label: str | None, related: list[str],
             triaged: bool) -> dict:
    """File the item as a new issue; its priority comes from triage when the repo has it, else from
    the severity. Returns it as a candidate, so a later item of this run can match it."""
    body_file = root / ".workbench" / "state" / f"follow-up-{item['key']}.md"
    body_file.write_text(issue_body(item, args.source, args.pr, related,
                                    followup.finding_marker(item, args.source, args.pr)), encoding="utf-8")
    argv = ["issue", "create", "--title", item["title"], "--body-file", str(body_file)]
    labels = [label] if label else []
    if not triaged:
        priority = f"priority:{followup.severity_priority(item.get('severity'))}"
        if ensure_label(root, priority):
            labels.append(priority)
        else:
            print(f"wb: follow-up: cannot create label '{priority}'; filing '{item['key']}' untriaged")
    for name in labels:
        argv += ["--label", name]
    try:
        done = gh_run(root, *argv)
    finally:
        body_file.unlink(missing_ok=True)
    url = next((line.strip() for line in (done.stdout or "").splitlines() if "/issues/" in line), None)
    if done.returncode != 0 or not url:
        raise DedupeFailed(f"filing failed: {(done.stderr or done.stdout).strip()}")
    return {"number": int(url.rstrip("/").rsplit("/", 1)[-1]), "title": item["title"], "body": "",
            "state": "open", "state_reason": "", "closed_at": "", "url": url, "labels": labels,
            "author_association": "NONE"}


def loop_done(root: Path, pr: str | None, sha: str | None) -> int:
    """The planner's last act (#27): the relay closes nothing before this record exists for the PR."""
    if not pr or not re.fullmatch(r"\d+", str(pr).rsplit("/", 1)[-1]):
        print("wb: loop-state done needs --pr <number or url>", file=sys.stderr)
        return 2
    items = load_follow_ups(root)
    unfiled = [item["key"] for item in items if not item.get("url")]
    if unfiled:
        print(f"wb: loop-state done: follow-ups not filed yet: {', '.join(unfiled)}", file=sys.stderr)
        return 1
    record = {"pr": int(str(pr).rsplit("/", 1)[-1]), "sha": sha,
              "followUps": list(dict.fromkeys(item["url"] for item in items)), "at": time.time()}
    path = root / ".workbench" / "state" / "loop-done.json"
    from conductor import Lock, atomic_json, read_json, write_loop_state, repo_name, pr_number, pr_url, QueueError
    member_path = path.with_name('queue-member.json')
    if member_path.exists():
        try:
            member = read_json(member_path)
            repo = repo_name(member['repo'])
            if not re.fullmatch(r'\d+', str(pr)):
                try:
                    pr_url(pr, repo)
                except QueueError as err:
                    print(f'wb: loop-state done: invalid --pr: {err}', file=sys.stderr)
                    return 2
            url = f"https://github.com/{repo}/pull/{record['pr']}"
            loop_path = path.with_name('loop.json')
            previous = read_json(loop_path) if loop_path.exists() else {}
            identity = read_json(path.with_name('claude.json'))
            prior_pr = previous.get('pr')
            try:
                prior_valid = bool(prior_pr and pr_url(prior_pr, repo))
            except (ValueError, TypeError):
                prior_valid = False
            if not (previous.get('state') == 'pr-open' and prior_valid and
                    pr_number(prior_pr) == record['pr'] and
                    previous.get('loopId') == identity['sessionId'] and
                    previous.get('queue') == member['queue']):
                write_loop_state(root, 'pr-open', url)
        except (OSError, ValueError, KeyError, TypeError) as err:
            print(f'wb: loop-state done: queue report failed: {err}; '
                  'the conductor will adopt the PR from loop-done.json', file=sys.stderr)
    with Lock(path.with_name('loop-done.lock')):
        atomic_json(path, record)
    print(f"loop done: PR #{record['pr']}; {len(items)} follow-up(s)")
    return 0


def loop_done_no_pr(root: Path, reason: str | None, pr: str | None = None) -> int:
    """Complete a closed issue without a PR; publish the queue report before the close latch."""
    if pr is not None or not isinstance(reason, str) or not reason.strip():
        print('wb: loop-state done --no-pr requires --reason and cannot use --pr', file=sys.stderr)
        return 2
    branch = current_branch(root)
    number = issue_number(root, branch)
    member_path = root / '.workbench/state/queue-member.json'
    if member_path.exists():
        try:
            member = json.loads(member_path.read_text(encoding='utf-8-sig'))
            number = str(member['number'])
        except (OSError, ValueError, KeyError, TypeError) as err:
            print(f'wb: loop-state done --no-pr: invalid queue member: {err}', file=sys.stderr)
            return 2
    if number == '?' or not branch:
        print('wb: loop-state done --no-pr: cannot identify issue or branch', file=sys.stderr)
        return 2
    try:
        issue = gh_json('issue', 'view', number, '--json', 'state,stateReason', cwd=root)
        if not isinstance(issue, dict) or issue.get('state') not in ('OPEN', 'CLOSED'):
            raise ValueError('invalid issue state')
    except (OSError, ValueError, RuntimeError) as err:
        print(f'wb: loop-state done --no-pr: cannot read issue #{number}: {err}', file=sys.stderr)
        return 2
    if issue['state'] != 'CLOSED':
        print(f'wb: issue #{number} is still open: close it with its reason first', file=sys.stderr)
        return 1
    try:
        prs = gh_json('pr', 'list', '--head', branch, '--state', 'open', '--json', 'number', cwd=root)
        if not isinstance(prs, list):
            raise ValueError('invalid PR list')
    except (OSError, ValueError, RuntimeError) as err:
        print(f'wb: loop-state done --no-pr: cannot check open PRs: {err}', file=sys.stderr)
        return 2
    if prs:
        print(f'wb: loop-state done --no-pr: an open PR exists for {branch}', file=sys.stderr)
        return 1
    items = load_follow_ups(root)
    unfiled = [item['key'] for item in items if not item.get('url')]
    if unfiled:
        print(f"wb: loop-state done: follow-ups not filed yet: {', '.join(unfiled)}", file=sys.stderr)
        return 1
    if member_path.exists():
        from conductor import write_loop_state
        try:
            write_loop_state(root, 'closed', reason=reason.strip())
        except (OSError, ValueError, KeyError, TypeError) as err:
            print(f'wb: loop-state done --no-pr: queue report failed: {err}', file=sys.stderr)
            return 2
    record = dict(pr=None, noPr=True, issue=int(number), issueState='CLOSED',
                  stateReason=issue.get('stateReason'), reason=reason.strip(),
                  followUps=list(dict.fromkeys(item['url'] for item in items)), at=time.time())
    path = root / '.workbench/state/loop-done.json'
    try:
        from conductor import Lock, atomic_json
        with Lock(path.with_name('loop-done.lock')):
            atomic_json(path, record)
    except (OSError, ValueError) as err:
        prefix = 'queue report `closed` written; ' if member_path.exists() else ''
        print(f'wb: loop-state done --no-pr: {prefix}cannot record completion: {err}; '
              'rerun to record completion', file=sys.stderr)
        return 2
    print(f"loop done: issue #{number} closed without a PR; {len(items)} follow-up(s)")
    return 0


def gh_json(*args: str, cwd: Path | None = None):
    """ForeignRepo (a RuntimeError) for a write to another repo, before gh runs (#71)."""
    done = subprocess.run(gh_argv(cwd or checkout(), args), capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=cwd)
    if done.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:3])} failed: {(done.stderr or done.stdout).strip()}")
    return json.loads(done.stdout)


def fetch_pr(pr_ref: str, repo: str | None = None) -> tuple[dict, list[dict]]:
    """ForeignRepo when the PR is not in repo: the merge that follows the gate must land there (#71)."""
    pr = gh_json("pr", "view", pr_ref, "--json", PR_FIELDS)
    match = re.match(r"https://github\.com/([^/]+/[^/]+)/pull/(\d+)", pr.get("url") or "")
    if not match:
        raise RuntimeError(f"cannot tell the repository from PR url {pr.get('url')!r}")
    if repo and not same_repo(match[1], repo):
        raise ForeignRepo(f"PR {pr['url']} is in {match[1]}, not this workbench's repo {repo}")
    pages = gh_json("api", f"repos/{match[1]}/pulls/{match[2]}/comments", "--paginate", "--slurp")
    inline = [comment for page in pages for comment in page] if pages and isinstance(pages[0], list) else list(pages or [])
    return pr, inline


def cmd_merge_check(args: argparse.Namespace) -> int:
    if not re.fullmatch(r"[0-9a-fA-F]{40}", args.head or ""):
        print("wb: merge-check --head needs the full 40-character SHA the suite ran on", file=sys.stderr)
        return 2
    root = checkout()
    try:
        pr, inline = fetch_pr(args.pr, workbench_repo(root))
        checks = None
        # CI is classified only for the tested head: checks of another commit mean nothing (#32).
        if (pr.get("headRefOid") or "").lower() == args.head.lower() and pr.get("state") == "OPEN" \
                and pr.get("mergeStateStatus") in ("UNSTABLE", "BLOCKED"):
            checks = fetch_checks(args.pr)
    except ForeignRepo as err:
        print(f"repo: {err}")
        return 1
    except (RuntimeError, ValueError, OSError) as err:
        print(f"gh: {err}")
        return 1
    failures = merge_failures(pr, inline, args.head, root, checks)
    if failures:
        print("\n".join(failures))
        return 1
    print("ok")
    return 0


def now() -> float:
    return time.monotonic()


def pause(seconds: float) -> None:
    time.sleep(seconds)


def cmd_wait_mail(args: argparse.Namespace) -> int:
    """Wake on unread mail, including replies arriving before this process starts. Never consume it."""
    try:
        if not math.isfinite(args.interval) or not 0 < args.interval <= 3600:
            raise ValueError('--interval must be finite, greater than zero and at most 3600 seconds')
        if not math.isfinite(args.timeout) or not 0 <= args.timeout <= 1440:
            raise ValueError('--timeout must be finite and between 0 and 1440 minutes')
        root = os.environ.get('AI_HUB')
        if not root or not Path(root).expanduser().is_dir():
            raise ValueError('AI_HUB must name an existing mailbox directory')
        hub.reload_paths()
        box = args.box
        if box is None:
            box = os.environ.get('AI_BOX') or (hub.whoami() or {}).get('box') or 'claude'
        hub.box_dir(box)  # Validate without creating a missing inbox.
        started = now()
        while True:
            messages = []
            for path in hub.unread(box):
                try:
                    messages.append(hub.parse_message(path))
                except FileNotFoundError:
                    continue  # Another reader moved it after our listing.
            if messages:
                for message in messages:
                    print(f"NEW MAIL: {message['id']} {message.get('from', '?')} {message['subject']}")
                return 0
            remaining = args.timeout - (now() - started) / 60
            if remaining <= 0:
                print(f'no new mail in {args.timeout:g} minutes')
                return 3
            pause(min(args.interval, remaining * 60))
    except (OSError, ValueError) as err:
        print(f'wb: wait-mail: {err}', file=sys.stderr)
        return 2


def ci_progress(checks: dict) -> tuple[str, str]:
    """('waiting', why) before any check reported; ('running', why); ('done', summary)."""
    everything = checks.get("all") or []
    if not everything:
        return "waiting", "no check has reported for this head yet"
    considered, pending, failed, optional_failed = split_checks(checks)
    if pending:
        return "running", f"{len(pending)} check(s) running: " + ", ".join(c.get("name") or "?" for c in pending)
    passed = len([c for c in considered if c.get("bucket") == "pass"])
    summary = f"CI DONE: {passed} passed, {len(failed)} failed"
    return "done", summary + (f", {len(optional_failed)} optional failed" if optional_failed else "")


def cmd_wait_ci(args: argparse.Namespace) -> int:
    """Wait in the background until CI on this head is finished (#32). 0 done, 3 timeout, 4 the head
    changed or the PR is no longer open, 2 usage. A gh failure is retried, never taken as done."""
    try:
        if not re.fullmatch(r"[0-9a-fA-F]{40}", args.head or ""):
            raise ValueError("--head needs the full 40-character SHA")
        for name, value, low, high in (("--timeout", args.timeout, 0, 1440), ("--no-ci-grace", args.no_ci_grace, 0, 1440),
                                       ("--interval", args.interval, 1, 3600)):
            if not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f"{name} must be between {low} and {high}")
    except ValueError as err:
        print(f"wb: wait-ci: {err}", file=sys.stderr)
        return 2
    started = now()               # the grace runs from the first sight of this head (#32 build note 1)
    last = None
    while True:
        elapsed = now() - started
        try:
            view = gh_json("pr", "view", str(args.pr), "--json", "state,headRefOid")
            if view.get("state") != "OPEN":
                print(f"PR is {view.get('state')}, not OPEN")
                return 4
            if (view.get("headRefOid") or "").lower() != args.head.lower():
                print(f"head changed: the PR head is {view.get('headRefOid')}, not {args.head}")
                return 4
            state, text = ci_progress(fetch_checks(str(args.pr)))
        except (RuntimeError, ValueError, OSError) as err:
            state, text = "error", f"gh failed, retrying: {err}"
        if state == "done":
            print(text)
            return 0
        if state == "waiting" and elapsed >= args.no_ci_grace * 60:
            print(f"CI DONE: no CI reported for this head in {args.no_ci_grace:g} minutes")
            return 0
        if text != last:
            print(text, flush=True)
            last = text
        if elapsed >= args.timeout * 60:
            print(f"CI still not done after {args.timeout:g} minutes")
            return 3
        pause(min(args.interval, max(1.0, args.timeout * 60 - elapsed)))


def git_out(root: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {(done.stderr or done.stdout).strip()[:300]}")
    return done.stdout


# --- conflict UPDATE rounds (#90) ----------------------------------------------------------------
# A small conflict - at most mergeRounds.smallConflictHunks conflict regions, nothing changed outside
# them, no conflicted file that the review flagged - is not counted. update-check decides it from the
# remerge-diff and records it for the HEAD; merge-round reads that record, so the planner never declares
# a conflict small itself.

MARKER_OPEN = re.compile(r"<{7,}(?:\s|$)")
MARKER_CLOSE = re.compile(r">{7,}(?:\s|$)")
# A marker left in the result: git's own, exactly 7 wide, so a heading's `=============` underline is
# not one. In the diff only `<<<<<<<` and `>>>>>>>` are sure; a `=======` or `|||||||` (also a 7-letter
# heading's underline) is a marker only when neither parent had that line (markers_left).
MARKER_LEFT = re.compile(r"(?:<{7}|>{7})(?: |$)|={7}$|\|{7}(?: |$)")
MARKER_SURE = re.compile(r"(?:<{7}|>{7})(?: |$)")
# git's remerge-diff, whatever the user's config says about colour, context, prefixes, blank lines,
# submodules or signatures.
REMERGE_DIFF = ("-c", "core.quotepath=false", "-c", "diff.suppressBlankEmpty=false", "show", "--remerge-diff",
                "--no-color", "--no-ext-diff", "--no-textconv", "--no-relative", "--submodule=short",
                "--no-show-signature", "-U3", "--format=", "--src-prefix=a/", "--dst-prefix=b/", "HEAD")
TEXT_CONFLICTS = ("content", "add/add")       # the conflict types that leave marker regions
LOCATION_RANGE = re.compile(r":\d+(?:-\d+)?(?::\d+)?$")
COUNTED_ONCE = ("update", "conflict", "conflict-small")     # a HEAD is one round of these


def merge_round_limits() -> dict:
    """ROUND_LIMITS with mergeRounds.conflict over it (#90). Raises followup.SettingsError."""
    return {**ROUND_LIMITS, "conflict": followup.merge_round_settings(followup.read_config())["conflict"]}


def update_check_path(root: Path) -> Path:
    return root / ".workbench" / "state" / "update-check.json"


def location_file(text: str | None, token: bool = False) -> str:
    """A finding's location (`path:272-275`, `path:3:7`, `path`) as a path, or "" when it is not one.
    Any path counts (`app/[id]/page.tsx`, `src/my file.py`). A backticked `token` from a report must
    not start with `-`, and one with neither a dot nor a slash counts only with a line (`Makefile:3`),
    so a bare word such as `refuses` is no path."""
    lined = bool(LOCATION_RANGE.search((text or "").strip()))
    name = LOCATION_RANGE.sub("", followup.normalise_file(text)).strip()
    if token and (name.startswith("-") or not (lined or re.search(r"[./]", name))):
        return ""
    return name


def review_flagged_files(root: Path) -> set[str]:
    """Files the PR's review flagged: the location line under every finding heading of every revmux
    report (`limited` partials too), and every recorded follow-up's `file`."""
    files = set()
    sections = SEVERE_SECTIONS + MINOR_SECTIONS + APART_SECTIONS
    for path in sorted((root / ".workbench" / "review").glob("revmux-r*.md")):
        try:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        section, pending = None, False
        for line in text.splitlines():
            if line.startswith("## "):
                section, pending = line[3:].strip().casefold(), False
            elif line.startswith("### "):
                pending = section in sections
            elif pending and line.strip():
                pending = False            # the first non-empty line after the heading: the location
                files.update(name for token in re.findall(r"`([^`]+)`", line) if (name := location_file(token, token=True)))
    files.update(name for item in load_follow_ups(root) if (name := location_file(item.get("file"))))
    return files


def is_flagged(name: str, flagged: set[str]) -> bool:
    """Equal, or one is a `/`-boundary suffix of the other (a report may name a shorter path)."""
    return any(name == other or name.endswith("/" + other) or other.endswith("/" + name) for other in flagged)


def remerge_files(diff: str) -> list[dict]:
    """`git show --remerge-diff` split per file: {path, conflicts (git's `remerge CONFLICT` types),
    binary, hunks (each a list of lines)}."""
    files = []
    for line in diff.splitlines():
        line = line.rstrip("\r")
        if line.startswith("diff --git "):
            match = re.match(r'diff --git "?a/(.*?)"? "?b/(.*?)"?$', line)
            files.append({"path": match[2] if match else line[11:], "conflicts": [], "binary": False,
                          "hunks": [], "old": "", "new": ""})
        elif not files:
            continue
        elif files[-1]["hunks"] and line[:1] in (" ", "-", "+", "\\", ""):
            files[-1]["hunks"][-1].append(line or " ")          # an empty line is a blank context line
        elif line.startswith("@@"):
            files[-1]["hunks"].append([])
        elif match := re.match(r"remerge CONFLICT \(([^)]*)\)", line):
            files[-1]["conflicts"].append(match[1])
        elif line.startswith("Binary files "):
            files[-1]["binary"] = True
        elif line.startswith("--- a/"):
            files[-1]["old"] = line[6:].rstrip("\t").strip('"')        # git adds a TAB after a path with a space
        elif line.startswith("+++ b/"):
            files[-1]["new"] = line[6:].rstrip("\t").strip('"')
    for entry in files:
        entry["path"] = entry.pop("new") or entry.pop("old", "") or entry["path"]
        entry.pop("old", None)
    return files


def outside_regions(entry: dict) -> tuple[int, str]:
    """(conflict regions, the first problem or ""). Each change block - a run of `-`/`+` lines with no
    context between them - must stay in a conflict region of the old side: every `-` line inside one
    (markers included), every `+` line inside one or in a block that touched one. A marker left in the
    result is a problem too. The region state carries across hunks: a resolution that keeps a long side
    splits one region over two hunks. A block never does: each hunk ends the block it holds."""
    regions, inside, problem = 0, False, ""
    outside = f"the merge changes code outside the conflict regions in {entry['path']}"
    left = f"conflict markers left in {entry['path']}"
    block, touched = [], False
    for hunk in entry["hunks"]:
        for line in hunk + [" "]:                # a closing context line ends the hunk's last block
            tag, text = line[:1], line[1:]
            if tag == "\\":
                continue
            if tag == " ":
                if MARKER_SURE.match(text):
                    return regions, left
                if not touched and not all(block):
                    problem = problem or outside
                block, touched = [], False
            elif tag == "-":
                if MARKER_OPEN.match(text):
                    inside, regions = True, regions + 1
                if not inside:
                    return regions, outside
                touched = True
                if MARKER_CLOSE.match(text):
                    inside = False
            else:
                if MARKER_SURE.match(text):
                    return regions, left
                block.append(inside)
    return regions, problem or (left if inside else "")


def markers_left(path: str, show) -> bool:
    """Whether the merge's `path` holds a git marker line that neither parent had. A stray `=======`
    outside every hunk is in no diff (keeping both sides, deleting only `<<<<<<<` and `>>>>>>>`)."""
    result = [line.rstrip("\r") for line in show("HEAD", path).splitlines()]
    if not any(MARKER_LEFT.match(line) for line in result):
        return False
    before = {line.rstrip("\r") for rev in ("HEAD^1", "HEAD^2") for line in show(rev, path).splitlines()}
    return any(MARKER_LEFT.match(line) and line not in before for line in result)


def classify_merge(diff: str, flagged: set[str], small_limit: int, show=None) -> dict:
    """A non-empty remerge-diff: {"result": "small"|"counted", "hunks", "files", "reason"}. A
    structural problem is the reason first, then a flagged file, then the number of regions.
    `show(rev, path)` reads a file's text at a revision ("" when absent) for markers_left."""
    regions, conflicted, problem = 0, [], ""
    entries = remerge_files(diff)
    for entry in entries:
        if entry["conflicts"]:
            conflicted.append(entry["path"])
        other = [kind for kind in entry["conflicts"] if kind not in TEXT_CONFLICTS]
        if other:
            found = f"{entry['path']}: {other[0]} conflict"
        elif not entry["conflicts"]:
            found = f"{entry['path']} changed outside any conflict"
        elif entry["binary"] or not entry["hunks"]:
            found = f"{entry['path']}: binary conflict (no text hunk)"
        else:
            count, found = outside_regions(entry)
            regions += count
        problem = problem or found
    if not problem and (not entries or not regions):
        problem = "the remerge-diff could not be parsed"            # fail closed: never small by accident
    if not problem and show:
        problem = next((f"conflict markers left in {path}" for path in conflicted if markers_left(path, show)), "")
    record = {"hunks": regions, "files": conflicted}
    flagged_file = next((name for name in conflicted if is_flagged(name, flagged)), None)
    if problem:
        reason = problem
    elif flagged_file:
        reason = f"{flagged_file} was flagged by review"
    elif small_limit == 0:
        reason = "small conflicts are off (mergeRounds.smallConflictHunks is 0)"
    elif regions > small_limit:
        reason = f"{regions} conflict hunks > {small_limit}"
    else:
        return {"result": "small", **record, "reason": ""}
    return {"result": "counted", **record, "reason": reason}


def plural(count: int, word: str) -> str:
    return f"{count} {word}" + ("" if count == 1 else "s")


def cmd_update_check(args: argparse.Namespace) -> int:
    """After `UPDATED <sha>` (#32): HEAD must be exactly one new commit, a merge of the pinned base
    into the reviewed head, and the tree clean. Prints `update: clean` (git's own merge, nothing
    added) or `update: conflict` (a non-empty remerge-diff: resolved conflicts or anything else
    added in the merge - review it like a fix) with `(small: ...)` or `(counted: <reason>)` (#90), and
    records the result for this HEAD in state/update-check.json, which merge-round reads."""
    for name in ("reviewed", "base"):
        if not re.fullmatch(r"[0-9a-fA-F]{40}", getattr(args, name) or ""):
            print(f"wb: update-check --{name} needs a full 40-character SHA", file=sys.stderr)
            return 2
    root = checkout()
    reviewed, base = args.reviewed.lower(), args.base.lower()
    try:
        head = git_out(root, "rev-parse", "HEAD").strip()
        parents = git_out(root, "rev-list", "--parents", "-n", "1", "HEAD").split()[1:]
        extra = git_out(root, "rev-list", "HEAD", f"^{reviewed}", f"^{base}").split()
        dirty = git_out(root, "status", "--porcelain", "--untracked-files=no").strip()
        failures = []
        if len(parents) != 2:
            failures.append(f"HEAD {head[:12]} is not a merge commit ({len(parents)} parent(s)); "
                            "the update must be `git merge --no-ff <base sha>`, never a rebase")
        else:
            if parents[0] != reviewed:
                failures.append(f"the merge's first parent is {parents[0][:12]}, not the reviewed head {reviewed[:12]}")
            if parents[1] != base:
                failures.append(f"the merge's second parent is {parents[1][:12]}, not the base {base[:12]} from the UPDATE mail")
        if extra != [head]:
            failures.append(f"{len(extra)} new commit(s) besides the base's; exactly one (the merge) is allowed")
        if dirty:
            failures.append("the working tree has uncommitted changes")
        if failures:
            print("\n".join(f"update-check: {line}" for line in failures))
            return 1
        remerge = git_out(root, *REMERGE_DIFF).strip()
    except (RuntimeError, OSError) as err:
        print(f"update-check: {err}")
        return 1
    if remerge:
        invalid = ""
        try:
            small_limit = followup.merge_round_settings(followup.read_config())["smallConflictHunks"]
        except followup.SettingsError as err:          # fail closed: no conflict is small, and say why
            small_limit, invalid = 0, str(err)
            print(f"wb: update-check: {err} - the conflict is counted", file=sys.stderr)

        def show(rev: str, path: str) -> str:
            try:
                return git_out(root, "show", f"{rev}:{path}")
            except (RuntimeError, OSError):
                return ""                              # not in that revision

        verdict = classify_merge(remerge, review_flagged_files(root), small_limit, show)
        if invalid and verdict["reason"].startswith("small conflicts are off"):
            verdict["reason"] = f"mergeRounds is invalid ({invalid})"
        if verdict["result"] == "small":
            how = (f"small: {plural(verdict['hunks'], 'conflict hunk')} in {plural(len(verdict['files']), 'file')}; "
                   "not counted")
        else:
            how = f"counted: {verdict['reason']}"
        print(f"update: conflict ({how}) - review the resolution: git show --remerge-diff {head}")
    else:
        verdict = {"result": "clean", "hunks": 0, "files": [], "reason": ""}
        print(f"update: clean - git's own merge of {base[:12]} into {reviewed[:12]}, nothing added")
    from conductor import atomic_json
    atomic_json(update_check_path(root), {"head": head, "reviewed": reviewed, "base": base, **verdict})
    return 0


def rounds_path(root: Path) -> Path:
    return root / ".workbench" / "state" / "merge-rounds.json"


def merge_summary(record: dict, limits: dict) -> str:
    """The merge note's line (#90): `merge rounds: 1 clean update; conflicts: 2 small (uncounted), 1 counted of 3`."""
    def count(kind: str) -> int:
        return int(record.get(kind) or 0)
    parts = []
    if count("update"):
        parts.append(plural(count("update"), "clean update"))
    if count("conflict") or count("conflict-small"):
        small = [f"{count('conflict-small')} small (uncounted)"] if count("conflict-small") else []
        parts.append("conflicts: " + ", ".join(small + [f"{count('conflict')} counted of {limits['conflict']}"]))
    if count("ci-rerun"):
        parts.append(plural(count("ci-rerun"), "CI rerun"))
    if count("ci-fix"):
        parts.append(plural(count("ci-fix"), "CI fix round"))
    return "merge rounds: " + ("; ".join(parts) or "none")


def cmd_merge_round(args: argparse.Namespace) -> int:
    """Count one proved round of a kind for this PR (#32); refuse beyond its limit (exit 1: the
    human's). The counts reset when the PR number changes. A conflict that update-check found small
    for this HEAD is recorded as `conflict-small` and never refused (#90); a kind that contradicts
    update-check's record for this HEAD is refused (exit 2); an update or conflict round is counted
    once per HEAD. --summary prints the merge note's line."""
    root = checkout()
    number = int(str(args.pr).rsplit("/", 1)[-1]) if re.fullmatch(r"(?:.*/)?\d+", str(args.pr)) else None
    if number is None:
        print("wb: merge-round --pr needs a PR number or URL", file=sys.stderr)
        return 2
    summary = getattr(args, "summary", False)       # ci-rerun calls it with a Namespace of its own
    if bool(args.kind) == bool(summary):
        print("wb: merge-round needs --kind or --summary", file=sys.stderr)
        return 2
    limits = ROUND_LIMITS
    if summary or args.kind == "conflict":         # mergeRounds never blocks counting the other kinds
        try:
            limits = merge_round_limits()
        except followup.SettingsError as err:
            print(f"wb: merge-round: {err}", file=sys.stderr)
            return 2
    path = rounds_path(root)
    try:
        record = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        record = {}
    if not isinstance(record, dict) or record.get("pr") != number:
        record = {"pr": number}
    if summary:
        print(merge_summary(record, limits))
        return 0
    head = ""                                      # no HEAD: nothing to bind to, so it is counted
    if args.kind in ("update", "conflict"):        # the CI rounds are not bound to update-check
        try:
            head = git_out(root, "rev-parse", "HEAD").strip()
        except (RuntimeError, OSError):
            pass
    try:
        check = json.loads(update_check_path(root).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        check = None
    said = check.get("result") if isinstance(check, dict) and head and check.get("head") == head else None
    if args.kind == "update" and said in ("small", "counted"):
        print(f"wb: merge-round: update-check said conflict for {head[:12]} - count it with --kind conflict",
              file=sys.stderr)
        return 2
    if args.kind == "conflict" and said == "clean":
        print(f"wb: merge-round: update-check said clean for {head[:12]} - count it with --kind update",
              file=sys.stderr)
        return 2
    kind = "conflict-small" if args.kind == "conflict" and said == "small" else args.kind
    heads = record.get("heads") if isinstance(record.get("heads"), dict) else {}
    again = bool(head) and kind in COUNTED_ONCE and heads.get(kind) == head
    count = int(record.get(kind) or 0)
    note = f" (already counted for {head[:12]})" if again else ""
    if kind == "conflict-small":
        count += 0 if again else 1
        message = (f"conflict round (small, not counted): {count} small so far; "
                   f"{int(record.get('conflict') or 0)} of {limits['conflict']} counted rounds used for PR #{number}{note}")
    else:
        limit = limits[kind]
        if not again and count >= limit:
            print(f"{kind}: the limit of {limit} round(s) for PR #{number} is reached - this goes to the human")
            return 1
        count += 0 if again else 1
        message = f"{kind} round {count} of {limit} for PR #{number}{note}"
    if not again:
        record[kind] = count
        if head and kind in COUNTED_ONCE:
            record["heads"] = {**heads, kind: head}
        from conductor import atomic_json
        atomic_json(path, record)
    print(message)
    return 0


def failed_actions_runs(checks: dict) -> tuple[list[tuple[str, str | None, dict]], list[dict]]:
    """([(run id, job id, check)] for failed GitHub Actions checks, [failed checks from other CI])."""
    _, _, failed, _ = split_checks(checks)
    runs, external = [], []
    for check in failed:
        match = RUN_LINK.search(check.get("link") or "")
        if match:
            runs.append((match[1], match[2], check))
        else:
            external.append(check)
    return runs, external


def cmd_ci_log(args: argparse.Namespace) -> int:
    """The failing CI log for a FIX round (#32): the last lines of each failed job, in a file under
    .workbench/review/ that the FIX mail points at."""
    root = checkout()
    try:
        runs, external = failed_actions_runs(fetch_checks(str(args.pr)))
    except (RuntimeError, ValueError, OSError) as err:
        print(f"gh: {err}")
        return 1
    if not runs and not external:
        print("no failed checks")
        return 1
    parts, fetched = [], 0
    for run, job, check in runs:
        argv = gh_argv(root, ["run", "view", run, "--log-failed"] + (["--job", job] if job else []))
        done = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace")
        header = f"=== {check.get('name')} ({check.get('workflow') or 'Actions'}) - {check.get('link')}\n"
        if done.returncode != 0:
            parts.append(header + f"(gh run view failed: {(done.stderr or done.stdout or '').strip()[:300]})")
            continue
        fetched += 1
        parts.append(header + "\n".join((done.stdout or "").splitlines()[-args.lines:]))
    for check in external:
        parts.append(f"=== {check.get('name')} {check.get('state')} - external CI, no log here: {check.get('link')}")
    folder = root / ".workbench" / "review"
    folder.mkdir(parents=True, exist_ok=True)
    k = 1
    while (folder / f"ci-r{k}.log").exists():
        k += 1
    path = folder / f"ci-r{k}.log"
    path.write_text("\n\n".join(parts) + "\n", encoding="utf-8")
    print(path)
    if runs and not fetched:
        print("no job log could be fetched - it is not evidence yet; retry ci-log", file=sys.stderr)
        return 2
    return 0


RERUN_SHOWS_WITHIN = 120.0     # seconds for a started rerun's checks to show as pending


def round_used(root: Path, pr: str, kind: str) -> bool:
    try:
        record = json.loads(rounds_path(root).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return False
    number = int(str(pr).rsplit("/", 1)[-1]) if re.fullmatch(r"(?:.*/)?\d+", str(pr)) else None
    return isinstance(record, dict) and record.get("pr") == number and int(record.get(kind) or 0) >= ROUND_LIMITS[kind]


def cmd_ci_rerun(args: argparse.Namespace) -> int:
    """One rerun of the failed GitHub Actions jobs before a FIX round (#32). Exit 0: the rerun
    started, is counted, and its checks show as pending (so wait-ci cannot read the old results).
    Exit 1 (refused): the rerun is used, or nothing can be rerun (external CI) - go to a FIX round.
    Exit 2 (operational, retry ci-rerun): gh failed, or no rerun started; nothing is counted. After
    `rerun started`, exit 2 only means its checks did not show within RERUN_SHOWS_WITHIN - run wait-ci."""
    root = checkout()
    if round_used(root, args.pr, "ci-rerun"):
        print(f"ci-rerun: the limit of {ROUND_LIMITS['ci-rerun']} round(s) is reached - go to a FIX round with wb.py ci-log")
        return 1
    try:
        runs, external = failed_actions_runs(fetch_checks(str(args.pr)))
    except (RuntimeError, ValueError, OSError) as err:
        print(f"gh: {err} - retry ci-rerun")
        return 2
    if not runs:
        print("nothing to rerun: " + (", ".join(c.get("name") or "?" for c in external) or "no failed checks")
              + " - go to a FIX round with wb.py ci-log")
        return 1
    started = []
    for run in dict.fromkeys(run for run, _, _ in runs):
        done = subprocess.run(gh_argv(root, ["run", "rerun", run, "--failed"]), capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        print(f"rerun {run}: " + ("started" if done.returncode == 0 else f"failed: {(done.stderr or '').strip()[:200]}"))
        if done.returncode == 0:
            started.append(run)
    if not started:
        print("no rerun started; nothing counted - retry ci-rerun")
        return 2
    if cmd_merge_round(argparse.Namespace(pr=args.pr, kind="ci-rerun")) != 0:
        return 1
    # Until GitHub re-queues them, `gh pr checks` still shows the old failed results (r22 m1).
    deadline = now() + RERUN_SHOWS_WITHIN
    while True:
        try:
            showing = [c for c in fetch_checks(str(args.pr))["all"]
                       if check_pending(c) and (RUN_LINK.search(c.get("link") or "") or [None, None])[1] in started]
        except (RuntimeError, ValueError, OSError):
            showing = []
        if showing:
            print(f"rerun started: {len(showing)} check(s) pending again - wait for the relay's ci mail (or wb.py wait-ci)")
            return 0
        if now() >= deadline:
            print(f"rerun started, but its checks did not show as pending within {RERUN_SHOWS_WITHIN:.0f}s - "
                  "run wb.py wait-ci, then merge-check")
            return 2
        pause(10)


def main() -> int:
    # Redirected Windows streams may use cp1252/cp437; preserve IDs even when prose cannot encode.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            try:
                stream.reconfigure(errors='replace')
            except (ValueError, OSError):
                pass
    parser = argparse.ArgumentParser(prog="wb")
    subs = parser.add_subparsers(dest="command", required=True)
    p = subs.add_parser('loop-state', help='report a queue member phase without terminal access')
    p.add_argument('state', choices=['pr-open', 'blocked', 'resumed', 'done'])
    p.add_argument('--sha', help='done: the merged head SHA')
    p.add_argument('--pr')
    p.add_argument('--no-pr', action='store_true', help='done: closed issue with no PR')
    p.add_argument('--reason')
    p.add_argument('--environmental', action='store_true',
                   help='blocked: by a limited tool, low disk or memory, not a question; the member keeps its queue slot (#61)')
    p.add_argument('--needs-human', action='store_true',
                   help='blocked, in a -WaitOnLimit checkout: the reason names a limit, but not an agent usage '
                        'limit to wait out (a GitHub, CI or disk limit, a Codex warning chooser that could not fail over); the human must answer (#88)')
    p.set_defaults(func=cmd_loop_state)
    p = subs.add_parser('wait-limit', help='-WaitOnLimit: have the relay wait out a usage limit it did not detect (#88)')
    p.add_argument('--box', default='codex', choices=['codex', 'claude'], help='the limited agent (default: codex)')
    p.add_argument('--reason', required=True, help='what the pane shows, e.g. "kimi 5-hour usage limit"')
    p.set_defaults(func=cmd_wait_limit)
    p = subs.add_parser("revmux", help="run a revmux round in its own visible session")
    p.add_argument("--round", type=int, required=True)
    p.add_argument("--scope", help="scope file, relative to the clone or absolute (required, except that a "
                                   "--rerun reuses the recorded one)")
    p.add_argument("--profile", help="revmux profile (default: the one the launcher saved for this checkout)")
    p.add_argument("--rerun", action="store_true",
                   help="rerun round K that a reviewer's usage limit stopped (#77): the report is kept as "
                        "revmux-r<K>-limited-<n>.md and revmux runs as r<K>-<n>")
    p.add_argument("--after", type=int, metavar="MINUTES",
                   help="with --rerun: wait this long on screen first, for the reviewer's limit to reset")
    p.set_defaults(func=cmd_revmux)
    route.add_commands(subs)          # route-issue (#109)
    p = subs.add_parser("plan-check", help="with a Kimi implementer: is the plan Kimi-grade? (#77)")
    p.add_argument("--plan", help="default .workbench/plan.md")
    p.set_defaults(func=cmd_plan_check)
    p = subs.add_parser("settings", help="print this checkout's settings (implementer, revmux profile, auto-merge, review, failover)")
    p.set_defaults(func=cmd_settings)
    p = subs.add_parser("handover", help="facts for a HANDOVER mail to a new implementer (#24)")
    p.set_defaults(func=cmd_handover)
    p = subs.add_parser("follow-up", help="record and file follow-up issues (#27)")
    follow = p.add_subparsers(dest="action", required=True)
    q = follow.add_parser("add", help="record a deferred finding or an out-of-scope plan item")
    q.add_argument("--key", required=True, help="a stable id, e.g. r2-m1 or plan-queue-switch")
    q.add_argument("--title", required=True)
    q.add_argument("--body-file")
    q.add_argument("--severity", required=True, choices=SEVERITIES)
    q.add_argument("--origin", required=True, help='"review r<K>" or "plan"')
    q.add_argument("--disputed", action="store_true")
    q.add_argument("--file", help="path[:line] the finding is about (#42)")
    q.add_argument("--own-issue", action="store_true", help="file this item separately even when minor")
    q.set_defaults(func=cmd_follow_up_add)
    q = follow.add_parser("file", help="file every recorded follow-up that has no issue yet")
    q.add_argument("--source", required=True, type=int, help="the issue this loop works on")
    q.add_argument("--pr", type=int, help="the loop's PR; required while followUp.dedupe is on (#42)")
    q.set_defaults(func=cmd_follow_up_file)
    p = subs.add_parser("merge-check", help="read-only: exit 0 and print ok only when the PR may be auto-merged")
    p.add_argument("--pr", required=True, help="PR number or URL")
    p.add_argument("--head", required=True, help="the full SHA the whole suite passed on")
    p.set_defaults(func=cmd_merge_check)
    p = subs.add_parser("review-round", help="record a verified revmux round's decision: continue, stop, clean, cap, or limit (#64, #77)")
    p.add_argument("--round", type=int)
    p.add_argument("--report", help="default .workbench/review/revmux-r<K>.md")
    p.add_argument("--severe", type=int, help="the verified Blocker+Critical+Major count, when it differs from revmux's")
    p.add_argument("--reason", help="why --severe is below revmux's count (required then)")
    p.add_argument("--summary", action="store_true", help="print the review line for the PR body or merge note")
    p.set_defaults(func=cmd_review_round)
    p = subs.add_parser("suite", help="run a long command (the whole suite, a build) in its own visible session (#45)")
    p.add_argument("--label", required=True, help="names the session and the log, e.g. the head's short sha")
    p.add_argument("--to", help="the mailbox the result goes to (default: AI_BOX, else claude)")
    p.add_argument("command", nargs=argparse.REMAINDER,
                   help="-- then the command and its arguments; no shell: the first word is found on PATH, "
                        "a .ps1 runs under pwsh, a .cmd/.bat (npm, gradlew) through cmd /d /s /c - so its "
                        "arguments may not contain cmd metacharacters (& | < > ^ %% ! \" ( )): they are refused")
    p.set_defaults(func=cmd_suite)
    p = subs.add_parser("human-review", help="open revdiff for the human, selected")
    p.add_argument("--base", required=True, help="e.g. origin/main")
    p.set_defaults(func=cmd_human_review)
    p = subs.add_parser("status", help="set this pane's sidebar status")
    p.add_argument("state", choices=["idle", "active", "blocked", "completed"])
    p.add_argument("--sound", action="store_true")
    p.set_defaults(func=cmd_status)
    p = subs.add_parser('wait-mail', help='wait for unread mail without using the terminal')
    p.add_argument('--box', help='mailbox (default: AI_BOX, pane registry entry, or claude)')
    p.add_argument('--timeout', type=float, default=55, metavar='MIN', help='timeout, 0..1440 minutes (default: 55)')
    p.add_argument('--interval', type=float, default=10, metavar='SEC', help='poll interval, >0..3600 seconds (default: 10)')
    p.set_defaults(func=cmd_wait_mail)
    p = subs.add_parser("wait-ci", help="wait in the background until CI on the tested head is done (#32)")
    p.add_argument("--pr", required=True)
    p.add_argument("--head", required=True, help="the full SHA pushed")
    p.add_argument("--timeout", type=float, default=90, metavar="MIN", help="default 90 minutes")
    p.add_argument("--no-ci-grace", type=float, default=5, metavar="MIN",
                   help="done with no CI when no check reported for this long (default 5)")
    p.add_argument("--interval", type=float, default=60, metavar="SEC", help="poll interval (default 60)")
    p.set_defaults(func=cmd_wait_ci)
    p = subs.add_parser("update-check", help="prove an UPDATE round is one merge of the pinned base (#32)")
    p.add_argument("--reviewed", required=True, help="the reviewed head's full SHA")
    p.add_argument("--base", required=True, help="the base SHA named in the UPDATE mail")
    p.set_defaults(func=cmd_update_check)
    p = subs.add_parser("merge-round", help="count one proved merge round; refuse beyond its limit (#32)")
    p.add_argument("--pr", required=True)
    p.add_argument("--kind", choices=sorted(ROUND_LIMITS))
    p.add_argument("--summary", action="store_true", help="print the merge note's line of this PR's rounds (#90)")
    p.set_defaults(func=cmd_merge_round)
    p = subs.add_parser("ci-log", help="write the failed CI jobs' logs for a FIX round (#32)")
    p.add_argument("--pr", required=True)
    p.add_argument("--lines", type=int, default=150)
    p.set_defaults(func=cmd_ci_log)
    p = subs.add_parser("ci-rerun", help="rerun the failed Actions jobs once, counted (#32)")
    p.add_argument("--pr", required=True)
    p.set_defaults(func=cmd_ci_rerun)
    args = parser.parse_args()
    try:
        return args.func(args)
    except agw.CtlError as err:
        print(f"wb: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
