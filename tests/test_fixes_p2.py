from __future__ import annotations

import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

import adk_dev_flow


class TestFixesP2(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]

    def test_n5_no_runtime_warning_on_module_execution(self) -> None:
        """N5: Running python -m adk_dev_flow.dashboard or mcp_server must NOT emit RuntimeWarning in stderr."""
        res = subprocess.run(
            [sys.executable, "-m", "adk_dev_flow.dashboard", "--help"],
            capture_output=True,
            text=True,
            cwd=str(self.repo_root),
        )
        self.assertNotIn("RuntimeWarning", res.stderr)

    def test_n6_manifest_versions_strict_consistency(self) -> None:
        """N6: Version across pyproject.toml, __init__.py, plugin.json, and marketplace.json must match."""
        pyproject_text = (self.repo_root / "pyproject.toml").read_text(encoding="utf-8")
        match = re.search(r'version\s*=\s*"([^"]+)"', pyproject_text)
        self.assertIsNotNone(match)
        expected_version = match.group(1)

        # __init__.__version__
        self.assertEqual(adk_dev_flow.__version__, expected_version)

        # plugin/adk-dev-flow/plugin.json
        p1 = self.repo_root / "plugin" / "adk-dev-flow" / "plugin.json"
        if p1.exists():
            data1 = json.loads(p1.read_text(encoding="utf-8"))
            self.assertEqual(data1.get("version"), expected_version)

        # plugin/adk-dev-flow/.codebuddy-plugin/plugin.json
        p2 = self.repo_root / "plugin" / "adk-dev-flow" / ".codebuddy-plugin" / "plugin.json"
        if p2.exists():
            data2 = json.loads(p2.read_text(encoding="utf-8"))
            self.assertEqual(data2.get("version"), expected_version)

        # .codebuddy-plugin/marketplace.json
        m = self.repo_root / ".codebuddy-plugin" / "marketplace.json"
        if m.exists():
            m_data = json.loads(m.read_text(encoding="utf-8"))
            self.assertEqual(m_data["plugins"][0]["version"], expected_version)


if __name__ == "__main__":
    unittest.main()
