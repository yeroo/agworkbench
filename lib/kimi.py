#!/usr/bin/env python3
"""kimi - what the Kimi Code implementer pane needs besides the executable (#65).

Kimi Code 2.1.1 has no per-session tool policy the workbench can set: `--agent-file` and `--agent`
are ignored in its interactive mode (only `-p` binds the profile; the TUI session always binds the
default `agent` profile), the workspace tool gate is a no-op, and the only file-based switch is the
global `[tools] disabled` in its config.toml. So:

- the role goes in `<checkout>/.kimi-code/AGENTS.md`, which Kimi loads next to the repository's own
  AGENTS.md (generated at every launch, excluded through `.git/info/exclude`);
- `git push` and `gh` are refused by shell shims that BASH_ENV puts first on PATH in every bash Kimi
  starts, with GIT_CONFIG_* pushInsteadOf and an unusable GH_TOKEN behind them for the spellings a
  shim cannot catch;
- the web tools are the human's own `[tools] disabled`, which `web-guard` checks and never writes;
- Kimi's "Trust this folder?" dialog would stop every new clone (Esc means exit), so the trust
  record is written the way Kimi writes it when you choose "Trust this folder".

Everything Kimi keeps lives under KIMI_CODE_HOME, else ~/.kimi-code.

  kimi.py web-guard [--allow-network]                 exit 1 with the reason when the guard is missing
  kimi.py prepare --checkout DIR --issue REF [--allow-network] [--dry-run]    prints JSON: env and paths
"""

from __future__ import annotations

import argparse
import hashlib
import json
import ntpath
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TEMPLATES = ROOT / "kimi"
WEB_TOOLS = ("FetchURL", "WebSearch")
MARKER = "<!-- agworkbench: the Kimi implementer's role"
AGENTS_MD = Path(".kimi-code") / "AGENTS.md"
EXCLUDE_LINE = "/.kimi-code/AGENTS.md"
REFUSED_SCHEME = "agworkbench-push-refused://"
# Every spelling of a GitHub remote git itself writes. Userinfo variants (https://user@github.com/...)
# do not match these prefixes; the git shim refuses those pushes, and the role forbids pushing at all.
GITHUB_PUSH_URLS = ("https://github.com/", "http://github.com/", "git@github.com:", "ssh://git@github.com/",
                    "ssh://github.com/")
REFUSED_TOKEN = "agworkbench-refused"
WIN_SHAPED = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|//)")


class Refused(RuntimeError):
    """The pane cannot start Kimi safely; the message says what to do."""


def kimi_home(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    configured = (env.get("KIMI_CODE_HOME") or "").strip()
    return Path(configured) if configured else Path.home() / ".kimi-code"


# --- workspace trust -------------------------------------------------------------------------

def _slug(name: str) -> str:
    """Kimi's slugifyWorkDirName."""
    slug = re.sub(r"[^a-z0-9._-]+", "-", name.lower()).strip("-")[:40].strip("-")
    return "workspace" if slug in ("", ".", "..") else slug


def trust_key(directory: str) -> str:
    """Kimi's trustKey: encodeWorkDirKey(canonicalWorkspaceRoot(root)). A Windows path is resolved,
    given forward slashes and lowercased; the key is wd_<slug of its last part>_<sha256[:12]>."""
    if WIN_SHAPED.match(directory):
        root = ntpath.normpath(ntpath.abspath(directory)).replace("\\", "/").rstrip("/").lower()
    else:
        root = os.path.abspath(directory).rstrip("/")
    return f"wd_{_slug(root.split('/')[-1] or root)}_{hashlib.sha256(root.encode('utf-8')).hexdigest()[:12]}"


def grant_trust(directory: str, home: Path | None = None) -> str:
    """Record Kimi's folder trust for one clone this tool created, only if it is not there yet; no
    other record is read or changed."""
    home = home or kimi_home()
    path = home / "workspace-trust" / trust_key(directory)
    if path.exists():
        return f"already trusted: {directory}"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".agworkbench-{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"root": directory, "trustedAt": int(time.time() * 1000)}), encoding="utf-8")
    os.replace(tmp, path)
    return f"trusted: {directory}"


# --- the web tools ---------------------------------------------------------------------------

def web_guard(allow_network: bool, home: Path | None = None) -> str | None:
    """None when the web tools are off (or the human allowed the network); otherwise why not, and
    what to add. Reads config.toml and never writes it: it is the human's, and global."""
    if allow_network:
        return None
    path = (home or kimi_home()) / "config.toml"
    fix = (f'add to {path}:\n\n  [tools]\n  disabled = ["FetchURL", "WebSearch"]\n\n'
           'or, if [tools] is already there, add both names to its disabled list. That turns them off for '
           'every Kimi session on this machine. Or set "allowNetwork": true in ~/.agworkbench.json to let '
           'the implementer use the web.')
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return f"Kimi's web tools are on: {path} does not exist. Kimi 2.1.1 ignores --agent-file, so the only way to turn them off is its config: {fix}"
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as err:
        return f"cannot read {path} to check that Kimi's web tools are off: {err}"
    tools = data.get("tools")
    disabled = tools.get("disabled") if isinstance(tools, dict) else None
    if disabled is not None and not (isinstance(disabled, list) and all(isinstance(x, str) for x in disabled)):
        return f"[tools] disabled in {path} is not a list of tool names: {disabled!r}"
    missing = [tool for tool in WEB_TOOLS if tool not in (disabled or [])]
    if missing:
        return (f"Kimi's web tools {', '.join(missing)} are on. Kimi 2.1.1 ignores --agent-file, so the only way "
                f"to turn them off is its config: {fix}")
    return None


# --- the pane's files and environment --------------------------------------------------------

def msys_path(path: str) -> str:
    """C:\\x\\y -> /c/x/y, the spelling Git Bash uses on PATH."""
    text = str(path).replace("\\", "/")
    drive = re.match(r"^([A-Za-z]):/(.*)$", text)
    return f"/{drive.group(1).lower()}/{drive.group(2)}" if drive else text


def git_bash(git: str | None) -> str:
    """The Git Bash Kimi would find itself (locateWindowsGitBash): <root>\\bin\\bash.exe next to the
    git.exe on PATH. Pinned through KIMI_SHELL_PATH so we know which shell runs, and BASH_ENV works."""
    if not git:
        raise Refused("git is not on PATH, so neither Kimi's shell (Git Bash) nor its git shim can be set up")
    if os.name != "nt":
        # Linux and macOS (#60): Kimi runs the system bash, which reads BASH_ENV like Git Bash does.
        bash = shutil.which("bash") or "/bin/bash"
        if not Path(bash).is_file():
            raise Refused("bash was not found; Kimi's shell tool needs it")
        return bash
    root = Path(git).resolve().parent.parent          # <root>\cmd\git.exe or <root>\bin\git.exe
    # ...or <root>\mingw64\bin\git.exe, one level deeper
    for candidate in (root / "bin" / "bash.exe", root.parent / "bin" / "bash.exe"):
        if candidate.is_file():
            return str(candidate)
    raise Refused(f"Git Bash (bin\\bash.exe) was not found next to {git}; Kimi's shell tool needs it")


def _render(template: Path, values: dict[str, str]) -> str:
    text = template.read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace(f"@@{key}@@", value)
    return text


def _write_lf(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".agworkbench-{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(text)
    os.replace(tmp, path)


def _tracked(checkout: Path, relative: Path) -> bool:
    done = subprocess.run(["git", "-C", str(checkout), "ls-files", "--error-unmatch", "--", relative.as_posix()],
                          capture_output=True, text=True)
    return done.returncode == 0


def _exclude(checkout: Path) -> None:
    path = checkout / ".git" / "info" / "exclude"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    if EXCLUDE_LINE in lines:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as stream:
        if lines and path.read_bytes()[-1:] not in (b"\n", b""):
            stream.write("\n")
        stream.write(EXCLUDE_LINE + "\n")


def environment(checkout: Path, bash: str) -> dict[str, str]:
    state = checkout / ".workbench" / "state"
    env = {
        "KIMI_CODE_NO_AUTO_UPDATE": "1",
        "KIMI_SHELL_PATH": bash,
        "BASH_ENV": str(state / "kimi-bin" / "env.sh").replace("\\", "/"),
        "GH_TOKEN": REFUSED_TOKEN,
        "GIT_CONFIG_COUNT": str(len(GITHUB_PUSH_URLS)),
    }
    for index, url in enumerate(GITHUB_PUSH_URLS):
        env[f"GIT_CONFIG_KEY_{index}"] = f"url.{REFUSED_SCHEME}.pushInsteadOf"
        env[f"GIT_CONFIG_VALUE_{index}"] = url
    return env


def prepare(checkout: str, issue: str, allow_network: bool, dry_run: bool = False,
            git: str | None = None, home: Path | None = None) -> dict:
    """Write the role, the shims and the trust record for this clone (unless dry_run), and return
    the environment the pane sets for Kimi."""
    root = Path(checkout).resolve()
    git = git if git is not None else shutil.which("git")
    bash = git_bash(git)
    role = root / AGENTS_MD
    if role.exists() or _tracked(root, AGENTS_MD):
        if _tracked(root, AGENTS_MD):
            raise Refused(f"{AGENTS_MD.as_posix()} is tracked in this repository; the Kimi implementer's role cannot "
                          "go there without changing it. Start this issue with another implementer.")
        if not role.read_text(encoding="utf-8", errors="replace").startswith(MARKER):
            raise Refused(f"{role} exists and was not written by agworkbench; move it away to start the Kimi implementer")
    shim_dir = root / ".workbench" / "state" / "kimi-bin"
    network = ("The human allowed the network: you may use FetchURL and WebSearch." if allow_network else
               "The web tools (FetchURL, WebSearch) are off: the human has not allowed the network.")
    result = {"agentsMd": str(role), "shimDir": str(shim_dir), "bash": bash,
              "env": environment(root, bash), "trust": None, "trustKey": trust_key(str(root))}
    if dry_run:
        return result
    _write_lf(role, _render(TEMPLATES / "AGENTS.md", {"ISSUE": issue, "NETWORK": network}))
    _exclude(root)
    # The shims hold these inside single quotes: an apostrophe in a path (an O'Neil profile) would
    # end the string, so each ' becomes '\'' (FIX r2 m5).
    values = {name: value.replace("'", "'\\''") for name, value in
              {"REAL_GIT": msys_path(git), "SHIM_DIR": msys_path(str(shim_dir))}.items()}
    for name in ("git", "gh", "env.sh"):
        _write_lf(shim_dir / name, _render(TEMPLATES / "bin" / name, values))
        if name != "env.sh" and os.name != "nt":
            (shim_dir / name).chmod(0o755)  # found on PATH only when executable, off Windows (#60)
    result["trust"] = grant_trust(str(root), home)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kimi")
    sub = parser.add_subparsers(dest="command", required=True)
    guard = sub.add_parser("web-guard")
    guard.add_argument("--allow-network", action="store_true")
    prep = sub.add_parser("prepare")
    prep.add_argument("--checkout", required=True)
    prep.add_argument("--issue", required=True)
    prep.add_argument("--allow-network", action="store_true")
    prep.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "web-guard":
            problem = web_guard(args.allow_network)
            if problem:
                print(problem)
                return 1
            print("ok")
            return 0
        print(json.dumps(prepare(args.checkout, args.issue, args.allow_network, args.dry_run)))
        return 0
    except (Refused, OSError, ValueError) as err:
        print(f"{err}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
