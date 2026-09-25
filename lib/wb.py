#!/usr/bin/env python3
"""wb - open the workbench's visible helper sessions without composing terminal commands by hand.

  wb.py revmux --round 1 --scope .workbench/review/scope-r1.md    # review round, own session
  wb.py human-review --base origin/main                          # revdiff, selected, for the human
  wb.py status blocked --sound                                    # this pane's sidebar status
  wb.py wait-mail                                                 # background inbox waiter
  wb.py settings                                                  # implementer, revmux profile, auto-merge, failover
  wb.py handover                                                  # open request, branch, git status (#24)
  wb.py follow-up add --key r2-m1 --title T --severity minor --origin "review r2"   (#27)
  wb.py follow-up file --source 27 --pr 30                        # file every unfiled follow-up (#27)
  wb.py loop-state done --pr 30 --sha <sha>                       # the planner's last act (#27)
  wb.py merge-check --pr 12 --head <sha>                          # read-only auto-merge gate (#23)
  wb.py wait-ci --pr 12 --head <sha>                              # background: until CI on the head is done (#32)
  wb.py update-check --reviewed <sha> --base <sha>                # an UPDATE round is one merge of the base (#32)
  wb.py merge-round --pr 12 --kind update                         # count a proved round; refuse past the limit (#32)
  wb.py ci-rerun --pr 12                                          # rerun the failed Actions jobs once (#32)
  wb.py ci-log --pr 12                                            # the failed jobs' log for a FIX round (#32)

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
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import agw  # noqa: E402
import hub  # noqa: E402


def checkout() -> Path:
    hub = os.environ.get("AI_HUB")
    if not hub:
        raise SystemExit("wb: AI_HUB is not set - run this inside an agworkbench pane")
    return Path(hub).resolve().parent


def issue_number(root: Path) -> str:
    branch = subprocess.run(["git", "-C", str(root), "branch", "--show-current"],
                            capture_output=True, text=True).stdout.strip()
    match = re.match(r"issue-(\d+)", branch)
    return match.group(1) if match else "?"


def pane_command(script: str, **params: str) -> str:
    """A helper's command line for agwinterm's direct mode: Windows quoting, no shell around it (#33).
    When the helper ends, its pane stays on screen with its input closed, so the close can prove it
    untouched (closer.py)."""
    shell = shutil.which("pwsh") or shutil.which("powershell.exe") or "powershell.exe"
    parts = [shell, "-NoLogo", "-ExecutionPolicy", "Bypass", "-File", str(HERE / script)]
    for key, value in params.items():
        parts += [f"-{key}", value]
    return subprocess.list2cmdline(parts)


def open_session(name: str, cwd: Path, command: str, select: bool) -> str:
    args = {"name": name, "cwd": str(cwd), "command": command, "command-mode": "direct"}
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
    """The profile the launcher resolved for this checkout (#20): claude-only when Claude is the
    implementer and Codex may be out of quota, comprehensive otherwise, or the human's revmuxProfile."""
    return checkout_settings(root)["revmuxProfile"]


def cmd_revmux(args: argparse.Namespace) -> int:
    root = checkout()
    scope = (root / args.scope).resolve() if not Path(args.scope).is_absolute() else Path(args.scope)
    if not scope.is_file():
        raise SystemExit(f"wb: scope file not found: {scope}")
    command = pane_command("run-revmux.ps1", Checkout=str(root), ScopeFile=str(scope),
                           Round=str(args.round), Profile=args.profile or revmux_profile(root))
    sid = open_session(f"#{issue_number(root)} revmux r{args.round}", root, command, select=False)
    print(f"revmux round {args.round} running in session {sid}; the report will arrive as mail")
    return 0


def cmd_human_review(args: argparse.Namespace) -> int:
    root = checkout()
    command = pane_command("human-review.ps1", Checkout=str(root), Base=args.base)
    sid = open_session(f"#{issue_number(root)} your review", root, command, select=True)
    print(f"human review open in session {sid}; annotations will arrive as mail from 'human'")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    agw.set_status(args.state, sound=args.sound, blink=args.sound)
    return 0


def cmd_loop_state(args: argparse.Namespace) -> int:
    # Reports stay in this checkout; the conductor alone owns the global queue.
    if args.state == 'done':
        return loop_done(checkout(), args.pr, args.sha)
    from conductor import write_loop_state
    try:
        write_loop_state(checkout(), args.state, args.pr, args.reason)
        return 0
    except (OSError, ValueError, KeyError, TypeError) as err:
        print(f'wb: loop-state: {err}', file=sys.stderr)
        return 2


def checkout_settings(root: Path) -> dict:
    """The checkout's settings record (state/implementer.json, #20 and #23), parsed once. Missing or
    invalid keys read as their defaults: a record written before #23 has no autoMerge, and that is off."""
    try:
        saved = json.loads((root / ".workbench" / "state" / "implementer.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        saved = {}
    if not isinstance(saved, dict):
        saved = {}
    tool = saved.get("tool") if saved.get("tool") in ("codex", "claude") else "codex"
    profile = saved.get("revmuxProfile")
    if not (isinstance(profile, str) and re.fullmatch(r"[A-Za-z0-9._-]+", profile)):
        profile = "comprehensive"
    return {"implementer": tool, "revmuxProfile": profile, "autoMerge": saved.get("autoMerge") is True,
            "autonomous": saved.get("autonomous") is True}


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
    print(f"implementer={settings['implementer']} revmuxProfile={settings['revmuxProfile']} "
          f"autoMerge={'true' if settings['autoMerge'] else 'false'} "
          f"autonomous={'true' if settings['autonomous'] else 'false'} "
          f"failover={'true' if failover_setting() else 'false'}")
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
ROUND_LIMITS = {"update": 3, "conflict": 1, "ci-rerun": 1, "ci-fix": 1}
RUN_LINK = re.compile(r"/actions/runs/(\d+)(?:/job/(\d+))?")


def gh_checks(pr_ref: str, required: bool = False) -> list[dict]:
    """`gh pr checks --json`: exit 1 (a check failed) and 8 (checks pending) still carry the JSON;
    "no checks reported" is no checks, not an error."""
    argv = ["gh", "pr", "checks", str(pr_ref), "--json", CHECK_FIELDS] + (["--required"] if required else [])
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
                + ", ".join(c.get("name") or "?" for c in pending) + ") - start wb.py wait-ci"]
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
    """Autonomous only (#27): every recorded follow-up is filed, and no Major+ review finding is
    deferred - disputed or not (r18: only Minor/Immaterial findings and plan items may be)."""
    if not checkout_settings(root)["autonomous"]:
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
            check_follow_ups(root))


# --- follow-up issues (#27) ----------------------------------------------------------------------
# One machine-readable list the planner fills as it defers findings or agrees a plan's out-of-scope
# items; `follow-up file` turns every unfiled item into a GitHub issue and writes the url back.

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
    path = follow_ups_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(items, indent=2), encoding="utf-8")
    os.replace(temporary, path)


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
        item.update(severity=args.severity, origin=args.origin, disputed=bool(args.disputed))
        save_follow_ups(root, items)
        print(f"{args.key}: already filed as {item['url']}; severity/origin/disputed updated")
        return 0
    item.update(title=args.title, body=body, severity=args.severity, origin=args.origin, disputed=bool(args.disputed))
    save_follow_ups(root, items)
    print(f"{args.key}: recorded ({args.severity}{', disputed' if args.disputed else ''})")
    return 0


def gh_run(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], cwd=str(root), capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def issue_body(item: dict, source: int, pr: int | None) -> str:
    where = f"Source: #{source}" + (f", PR #{pr}" if pr else "")
    return (f"{item.get('body', '').rstrip()}\n\n{where}\n"
            f"Severity: {item.get('severity')}; origin: {item.get('origin')}"
            f"{'; disputed' if item.get('disputed') else ''}\n\n"
            f"<!-- agworkbench:follow-up source=#{source} -->\n{PLANNER_MARKER}\n")


def cmd_follow_up_file(args: argparse.Namespace) -> int:
    root = checkout()
    items = load_follow_ups(root)
    pending = [item for item in items if not item.get("url")]
    if not pending:
        print("no unfiled follow-ups")
        return 0
    # One level of chaining at most: a follow-up of a follow-up is filed under another label.
    source = gh_run(root, "issue", "view", str(args.source), "--json", "labels")
    if source.returncode != 0:
        print(f"wb: follow-up: cannot read issue #{args.source}: {source.stderr.strip()}", file=sys.stderr)
        return 1
    labels = {label.get("name") for label in json.loads(source.stdout).get("labels", [])}
    label = NESTED_LABEL if labels & {FOLLOW_UP_LABEL, NESTED_LABEL} else FOLLOW_UP_LABEL
    created = gh_run(root, "label", "create", label, "--color", "BFD4F2",
                     "--description", "filed automatically by an agworkbench loop")
    use_label = created.returncode == 0 or "already exists" in (created.stderr or "")
    if not use_label:
        print(f"wb: follow-up: cannot create label '{label}' ({created.stderr.strip()}); filing without it")
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
            if use_label:
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
              "followUps": [item["url"] for item in items], "at": time.time()}
    path = root / ".workbench" / "state" / "loop-done.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    print(f"loop done: PR #{record['pr']}; {len(items)} follow-up(s)")
    return 0


def gh_json(*args: str):
    done = subprocess.run(["gh", *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if done.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:3])} failed: {(done.stderr or done.stdout).strip()}")
    return json.loads(done.stdout)


def fetch_pr(pr_ref: str) -> tuple[dict, list[dict]]:
    pr = gh_json("pr", "view", pr_ref, "--json", PR_FIELDS)
    match = re.match(r"https://github\.com/([^/]+/[^/]+)/pull/(\d+)", pr.get("url") or "")
    if not match:
        raise RuntimeError(f"cannot tell the repository from PR url {pr.get('url')!r}")
    pages = gh_json("api", f"repos/{match[1]}/pulls/{match[2]}/comments", "--paginate", "--slurp")
    inline = [comment for page in pages for comment in page] if pages and isinstance(pages[0], list) else list(pages or [])
    return pr, inline


def cmd_merge_check(args: argparse.Namespace) -> int:
    if not re.fullmatch(r"[0-9a-fA-F]{40}", args.head or ""):
        print("wb: merge-check --head needs the full 40-character SHA the suite ran on", file=sys.stderr)
        return 2
    root = checkout()
    try:
        pr, inline = fetch_pr(args.pr)
        checks = None
        # CI is classified only for the tested head: checks of another commit mean nothing (#32).
        if (pr.get("headRefOid") or "").lower() == args.head.lower() and pr.get("state") == "OPEN" \
                and pr.get("mergeStateStatus") in ("UNSTABLE", "BLOCKED"):
            checks = fetch_checks(args.pr)
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


def cmd_update_check(args: argparse.Namespace) -> int:
    """After `UPDATED <sha>` (#32): HEAD must be exactly one new commit, a merge of the pinned base
    into the reviewed head, and the tree clean. Prints `update: clean` (git's own merge, nothing
    added) or `update: conflict` (a non-empty remerge-diff: resolved conflicts or anything else
    added in the merge - review it like a fix)."""
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
        remerge = git_out(root, "show", "--remerge-diff", "--format=", "HEAD").strip()
    except (RuntimeError, OSError) as err:
        print(f"update-check: {err}")
        return 1
    if remerge:
        print(f"update: conflict - review the resolution: git show --remerge-diff {head}")
    else:
        print(f"update: clean - git's own merge of {base[:12]} into {reviewed[:12]}, nothing added")
    return 0


def rounds_path(root: Path) -> Path:
    return root / ".workbench" / "state" / "merge-rounds.json"


def cmd_merge_round(args: argparse.Namespace) -> int:
    """Count one proved round of a kind for this PR (#32); refuse beyond its limit (exit 1: the
    human's). The counts reset when the PR number changes."""
    root = checkout()
    number = int(str(args.pr).rsplit("/", 1)[-1]) if re.fullmatch(r"(?:.*/)?\d+", str(args.pr)) else None
    if number is None:
        print("wb: merge-round --pr needs a PR number or URL", file=sys.stderr)
        return 2
    path = rounds_path(root)
    try:
        record = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        record = {}
    if not isinstance(record, dict) or record.get("pr") != number:
        record = {"pr": number}
    limit, count = ROUND_LIMITS[args.kind], int(record.get(args.kind) or 0)
    if count >= limit:
        print(f"{args.kind}: the limit of {limit} round(s) for PR #{number} is reached - this goes to the human")
        return 1
    record[args.kind] = count + 1
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(record, indent=2), encoding="utf-8")
    os.replace(temporary, path)
    print(f"{args.kind} round {count + 1} of {limit} for PR #{number}")
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
        argv = ["gh", "run", "view", run, "--log-failed"] + (["--job", job] if job else [])
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
        done = subprocess.run(["gh", "run", "rerun", run, "--failed"], capture_output=True, text=True,
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
            print(f"rerun started: {len(showing)} check(s) pending again - start wb.py wait-ci")
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
    p.add_argument('--reason')
    p.set_defaults(func=cmd_loop_state)
    p = subs.add_parser("revmux", help="run a revmux round in its own visible session")
    p.add_argument("--round", type=int, required=True)
    p.add_argument("--scope", required=True, help="scope file, relative to the clone or absolute")
    p.add_argument("--profile", help="revmux profile (default: the one the launcher saved for this checkout)")
    p.set_defaults(func=cmd_revmux)
    p = subs.add_parser("settings", help="print this checkout's settings (implementer, revmux profile, auto-merge, failover)")
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
    q.set_defaults(func=cmd_follow_up_add)
    q = follow.add_parser("file", help="file every recorded follow-up that has no issue yet")
    q.add_argument("--source", required=True, type=int, help="the issue this loop works on")
    q.add_argument("--pr", type=int)
    q.set_defaults(func=cmd_follow_up_file)
    p = subs.add_parser("merge-check", help="read-only: exit 0 and print ok only when the PR may be auto-merged")
    p.add_argument("--pr", required=True, help="PR number or URL")
    p.add_argument("--head", required=True, help="the full SHA the whole suite passed on")
    p.set_defaults(func=cmd_merge_check)
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
    p.add_argument("--kind", required=True, choices=sorted(ROUND_LIMITS))
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
