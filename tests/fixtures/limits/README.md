# Usage-limit fixtures (#24)

`strings-codex.txt` and `strings-claude.txt` are what the installed binaries say, extracted on
2026-09-25 with:

```
python tests/fixtures/limits/extract.py \
  --codex  %APPDATA%\npm\node_modules\@openai\codex\node_modules\@openai\codex-win32-x64\vendor\x86_64-pc-windows-msvc\bin\codex.exe \
  --claude %USERPROFILE%\.local\bin\claude.exe
```

Each line is one printable run of bytes from the binary that contains a limit phrase. Some runs
are minified JavaScript (Claude Code) or several adjacent strings (Codex). That is expected: the
file is evidence, not a list of messages.

The `*.txt` frames are pane frames built around those strings. The limit row in every frame is
copied from the strings files, and `tests/test_limits.py` checks that. Claude composes
`You've hit your ${name}` from its limit-name table (`five_hour:"session limit"`, ...), so for those
rows the test checks the template and the name separately.

The layout around each row is **constructed**, not captured, from the shapes in the real frames in
`tests/frames.py`:
- Claude's composer between two rules, `●` items and `⎿` results;
- Codex's `›` composer and `•`/`└` history cells.

The Codex warning chooser (`codex-warning-chooser.txt`) quotes the rows the planner captured from
the real #68 pane on 2026-09-24. When a real limit frame is seen, add it here verbatim and keep the
constructed one only if it still adds a case.
