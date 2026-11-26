from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from adk_dev_flow.cli import init_target_repo


class TestCliInit(unittest.TestCase):
    def test_init_target_repo_scaffolds_templates_and_gitignore(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            actions = init_target_repo(target, include_ci=True, force=False)
            self.assertTrue(len(actions) >= 5)

            skill = target / ".codebuddy" / "skills" / "deterministic-dev" / "SKILL.md"
            mcp_cfg = target / ".codebuddy" / "mcp.json"
            settings_cfg = target / ".codebuddy" / "settings.json"
            ci_cfg = target / ".gitlab-ci.yml"
            gitignore = target / ".gitignore"

            self.assertTrue(skill.is_file())
            self.assertTrue(mcp_cfg.is_file())
            self.assertTrue(settings_cfg.is_file())
            self.assertTrue(ci_cfg.is_file())
            self.assertIn(".adk/state.db*", gitignore.read_text(encoding="utf-8"))
            self.assertIn(".adk/workflow.db*", gitignore.read_text(encoding="utf-8"))
            self.assertIn(".adk/exemptions.log", gitignore.read_text(encoding="utf-8"))
            self.assertIn(".adk/worktrees/", gitignore.read_text(encoding="utf-8"))

            # Verify valid JSON templates and secure defaults
            mcp_data = json.loads(mcp_cfg.read_text(encoding="utf-8"))
            self.assertEqual(mcp_data["mcpServers"]["adk-dev-flow"]["command"], "adk-dev-flow")
            self.assertEqual(
                mcp_data["mcpServers"]["adk-dev-flow"]["env"]["GITLAB_SSL_VERIFY"], "true"
            )
            settings_data = json.loads(settings_cfg.read_text(encoding="utf-8"))
            self.assertEqual(
                settings_data["hooks"]["PreToolUse"][0]["hooks"][0]["command"],
                "adk-workflow-guard",
            )

            # Second run without --force skips existing files and does not duplicate gitignore entries
            actions2 = init_target_repo(target, include_ci=True, force=False)
            self.assertTrue(all(a.startswith("skipped") for a in actions2))
            self.assertEqual(gitignore.read_text(encoding="utf-8").count(".adk/worktrees/"), 1)


if __name__ == "__main__":
    unittest.main()
