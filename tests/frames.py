"""Captured terminal frames and fakes, shared without importing test modules."""

# Verbatim captured frames supplied with plan v3 (Unicode preserved).

CLAUDE_IDLE = r"""✻ Brewed for 1m 0s · done 4:23 PM
─────────────────────────────────────────────────────
>
─────────────────────────────────────────────────────
  [Fable 5.1] C:\Users\boris\source\workbench\docxy-issue-50 on issue-50-suite-proje…
  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents"""

CLAUDE_RUNNING = r"""● Checking mcp/skill coupling, testing dirs, docs mentioning projcore and control verbs
· Hullaballooing… (2m 30s · ↓ 4.9k tokens · thinking some more with high effort)
  ⎿  Tip: Use /btw to ask a quick side question without interrupting Claude's current
     work
───────────────────────────────────────────────────────────────────────────────────────
>
───────────────────────────────────────────────────────────────────────────────────────
  [Fable 5.1] C:\Users\boris\source\workbench\docxy-issue-50 on issue-50-suite-proje…
  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents"""

CODEX_IDLE = r"""• No unread mail. Waiting for the next “Chat from Workbench:” notification; no files
  edited.
──────────────────────────────────────────────────
› Ask Codex to do anything
  gpt-6-astra high · ~\source\workbench\agworkbench-issue-1 · Wait for draft plan"""

CODEX_QUEUED = r"""• Explored
  └ Read SKILL.md (workbench-implementer skill)
───────────────────────────────────────────────────────────────────────────────────────
◦ Working (11s • esc to interrupt)
• Queued follow-up inputs
  ↳ Chat from Workbench: workbench mail from claude: plan v1 for #50 (step 1:
    projcore::editor) [id 20260922T193916Z-claude-1528] - read it with: python C:
    \Users\boris\source\agworkbench\lib\agmsg.py read 20260922T193916Z-claude-1528
    …
    alt + ↑ edit last queued message
› Ask Codex to do anything
  gpt-6-astra high · ~\source\workbench\docxy-issue-50 · Wait for implementation plan"""

CODEX_UNSUBMITTED = r"""──────────────────────────────────────────────────
› Chat from Workbench: workbench mail from claude: plan v1 for #50 step 2 (Project tab
  kind in the suite) [id 20260922T221152Z-claude-7be1] - read it with: python C:
  \Users\boris\source\agworkbench\lib\agmsg.py read 20260922T221152Z-claude-7be1
  (AI_HUB=C:\Users\boris\source\workbench\docxy-issue-50\.workbench)
  gpt-6-astra high · ~\source\workbench\docxy-issue-50 · Wait for implementation plan"""


TEXT = (r"Chat from Workbench: workbench mail from claude: plan v1 for #50 (step 1: "
        r"projcore::editor) [id 20260922T193916Z-claude-1528] - read it with: python C:\Users\boris\source\agworkbench\lib\agmsg.py "
        r"read 20260922T193916Z-claude-1528 (AI_HUB=C:\Users\boris\source\workbench\docxy-issue-50\.workbench)")


def codex(content, prefix=''):
    return prefix + '\n' + '─' * 50 + '\n› ' + content + '\n  gpt-6-astra high · fixture'


def claude(content):
    return CLAUDE_IDLE.replace('\n>\n', '\n> ' + content + '\n')


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def pause(self, seconds):
        self.t += seconds


class FakeAgw:
    def __init__(self, tool='codex', frames=None, after=None):
        self.tool = tool
        self.frames = list(frames) if frames else None
        self.after = after
        self.keys = []
        self.reads = 0
        self.read_errors = {}
        self.type_errors = {}

    def pane_text(self, pane):
        self.reads += 1
        if self.reads in self.read_errors:
            raise self.read_errors[self.reads]
        if self.frames:
            if len(self.frames) > 1:
                return self.frames.pop(0)
            return self.frames[0]
        if not self.keys:
            return CODEX_IDLE if self.tool == 'codex' else CLAUDE_IDLE
        if len(self.keys) == 1:
            return codex(self.keys[0]) if self.tool == 'codex' else claude(self.keys[0])
        if self.after:
            return self.after(self)
        return CODEX_IDLE if self.tool == 'codex' else CLAUDE_IDLE

    def type_into(self, pane, text):
        self.keys.append(text)
        if len(self.keys) in self.type_errors:
            raise self.type_errors[len(self.keys)]
