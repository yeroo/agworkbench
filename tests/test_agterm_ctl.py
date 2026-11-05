from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import agterm_ctl  # noqa: E402


class ForegroundShells(unittest.TestCase):
    """#98 on agterm: the relay's exit watch needs `foregroundShells`; agterm omits a pane's
    `foreground` exactly while that pane is at its shell prompt."""

    def test_a_pane_at_its_prompt_names_the_shell_and_a_running_one_is_empty(self):
        with mock.patch.dict(os.environ, {"SHELL": "/bin/bash"}):
            node = agterm_ctl._session_node({"id": "S1", "split": True, "foreground": None,
                                             "splitForeground": ["pwsh", "-File", "pane-implementer-claude.ps1"]})
        self.assertEqual(["bash", ""], node["foregroundShells"])
        self.assertEqual(len(node["paneIds"]), len(node["foregroundShells"]))

    def test_an_unsplit_session_running_its_agent(self):
        node = agterm_ctl._session_node({"id": "S2", "foreground": ["pwsh", "-File", "pane-claude.ps1"]})
        self.assertEqual([""], node["foregroundShells"])

    def test_no_shell_variable_falls_back_to_sh(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            node = agterm_ctl._session_node({"id": "S3"})
        self.assertEqual(["sh"], node["foregroundShells"])


if __name__ == "__main__":
    unittest.main()
