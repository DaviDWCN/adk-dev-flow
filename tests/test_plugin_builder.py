from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from adk_dev_flow.plugin_builder import (
    build_plugin_bundle,
    cli_main,
    extract_pyproject_version,
    generate_logo_svg,
    strip_yaml_frontmatter,
    sync_mcp_tool_schemas,
    sync_plugin_all,
)


class TestPluginBuilder(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parent.parent

    def test_strip_yaml_frontmatter(self) -> None:
        content_with_fm = "---\ntitle: Rules\nstatus: active\n---\n# Real Content\nLine 2"
        self.assertEqual(strip_yaml_frontmatter(content_with_fm), "# Real Content\nLine 2")

        content_without_fm = "# Direct Heading\nNo frontmatter here."
        self.assertEqual(strip_yaml_frontmatter(content_without_fm), content_without_fm)

    def test_extract_pyproject_version(self) -> None:
        version = extract_pyproject_version(self.repo_root)
        self.assertTrue(len(version) > 0)
        self.assertRegex(version, r"^\d+\.\d+\.\d+")

    def test_generate_logo_svg(self) -> None:
        svg = generate_logo_svg()
        self.assertTrue(svg.startswith("<svg"))
        self.assertIn('viewBox="0 0 256 256"', svg)
        self.assertTrue(svg.strip().endswith("</svg>"))

    def test_build_plugin_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "adk-dev-flow"
            res = build_plugin_bundle(self.repo_root, target)

            self.assertEqual(res["target_dir"], str(target.resolve()))
            self.assertTrue(target.is_dir())

            # 1. plugin.json
            plugin_json_file = target / "plugin.json"
            self.assertTrue(plugin_json_file.is_file())
            data = json.loads(plugin_json_file.read_text(encoding="utf-8"))
            self.assertEqual(data["name"], "adk-dev-flow")
            self.assertEqual(data["displayName"], "ADK Deterministic Dev Flow")
            self.assertIn("suggestedPrompts", data)
            self.assertTrue(len(data["suggestedPrompts"]) >= 3)
            self.assertEqual(data["logo"], "assets/logo.svg")

            # 2. mcp_config.json
            mcp_json_file = target / "mcp_config.json"
            self.assertTrue(mcp_json_file.is_file())
            mcp_data = json.loads(mcp_json_file.read_text(encoding="utf-8"))
            self.assertIn("adk-dev-flow", mcp_data["mcpServers"])
            server_cfg = mcp_data["mcpServers"]["adk-dev-flow"]
            self.assertEqual(server_cfg["command"], "pythonw")
            self.assertEqual(server_cfg["args"], ["-m", "adk_dev_flow.mcp_server"])

            # 2b. .mcp.json — CodeBuddy's plugin MCP discovery file
            cb_mcp_file = target / ".mcp.json"
            self.assertTrue(cb_mcp_file.is_file())
            self.assertEqual(json.loads(cb_mcp_file.read_text(encoding="utf-8")), mcp_data)

            # 1b. .codebuddy-plugin/plugin.json (CodeBuddy manifest layout)
            cb_manifest_file = target / ".codebuddy-plugin" / "plugin.json"
            self.assertTrue(cb_manifest_file.is_file())
            cb_data = json.loads(cb_manifest_file.read_text(encoding="utf-8"))
            self.assertEqual(cb_data["name"], "adk-dev-flow")

            # 3. assets/logo.svg
            logo_file = target / "assets" / "logo.svg"
            self.assertTrue(logo_file.is_file())
            self.assertIn("<svg", logo_file.read_text(encoding="utf-8"))

            # 4. skills/deterministic-dev/SKILL.md
            skill_file = target / "skills" / "deterministic-dev" / "SKILL.md"
            self.assertTrue(skill_file.is_file())
            skill_text = skill_file.read_text(encoding="utf-8")
            self.assertIn("deterministic-dev", skill_text)

            # 5. rules/AGENTS.md
            rules_file = target / "rules" / "AGENTS.md"
            self.assertTrue(rules_file.is_file())
            rules_text = rules_file.read_text(encoding="utf-8")
            # Must NOT start with YAML frontmatter delimiter
            self.assertFalse(rules_text.strip().startswith("---"))
            self.assertIn("Core Engineering Invariants", rules_text)
            self.assertIn("TDD Red Compliance", rules_text)

            # 6. README.md
            readme_file = target / "README.md"
            self.assertTrue(readme_file.is_file())
            self.assertIn("ADK Deterministic Dev Flow Plugin", readme_file.read_text(encoding="utf-8"))

            # 7. sidecars/dashboard/sidecar.json
            sidecar_file = target / "sidecars" / "dashboard" / "sidecar.json"
            self.assertTrue(sidecar_file.is_file())
            sidecar_data = json.loads(sidecar_file.read_text(encoding="utf-8"))
            self.assertTrue(sidecar_data.get("has_web_ui"))
            self.assertEqual(sidecar_data["ui_config"]["views"][0]["entrypoint"], "SIDECAR_UI_ENTRYPOINT_AUX_PANE")

            # 8. assets/dashboard.html
            dash_file = target / "assets" / "dashboard.html"
            self.assertTrue(dash_file.is_file())

    def test_sync_plugin_all_no_global(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_root = Path(tmp)
            # Scaffold minimal structure
            (fake_root / "src" / "adk_dev_flow" / "templates").mkdir(parents=True)
            (fake_root / "pyproject.toml").write_text('version = "0.9.9"\n', encoding="utf-8")
            (fake_root / "src" / "adk_dev_flow" / "templates" / "SKILL.md").write_text(
                "# Minimal Skill\n", encoding="utf-8"
            )
            (fake_root / "src" / "adk_dev_flow" / "templates" / "engineering_rules.md").write_text(
                "---\ntype: rule\n---\n# Rule Header\n", encoding="utf-8"
            )

            res = sync_plugin_all(fake_root, install_global=False)
            self.assertIsNone(res["global_plugin"])
            self.assertEqual(res["repo_plugin"]["version"], "0.9.9")
            self.assertTrue((fake_root / "plugin" / "adk-dev-flow" / "plugin.json").is_file())

    def test_sync_mcp_tool_schemas(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            custom_dir = Path(tmp) / "custom_mcp"
            written = sync_mcp_tool_schemas(extra_dirs=[custom_dir])
            self.assertTrue(len(written) >= 15)
            # Verify flow_preflight_test and flow_dashboard are present in written
            names = [Path(p).stem for p in written]
            self.assertIn("flow_preflight_test", names)
            self.assertIn("flow_dashboard", names)
            self.assertTrue((custom_dir / "flow_preflight_test.json").is_file())
            self.assertTrue((custom_dir / "flow_dashboard.json").is_file())

    def test_cli_main(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            exit_code = cli_main(["--repo-root", str(self.repo_root), "--no-global"])
            self.assertEqual(exit_code, 0)


if __name__ == "__main__":
    unittest.main()
