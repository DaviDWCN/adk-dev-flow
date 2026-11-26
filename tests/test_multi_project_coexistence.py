from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from adk_dev_flow.codebuddy_bridge import run_codebuddy_worker
from adk_dev_flow.dashboard import find_available_port
from adk_dev_flow.forge_client import GitLabClient, get_git_forge
from adk_dev_flow.mcp_server import create_mcp_server, load_project_config
from adk_dev_flow.plugin_builder import build_plugin_bundle


class TestMultiProjectCoexistence(unittest.TestCase):
    def test_gitlab_auto_detects_project_from_git_remote(self) -> None:
        """GitLabClient must auto-detect project path from git remote origin when GITLAB_PROJECT_ID is not set."""
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
            subprocess.run(
                ["git", "remote", "add", "origin", "http://10.94.188.18:8000/ht-yangdawei/uw-research-report-adk.git"],
                cwd=repo,
                check=True,
                capture_output=True,
            )

            # With no GITLAB_PROJECT_ID in env, GitLabClient must derive it from git remote
            with patch.dict(os.environ, {"GITLAB_URL": "http://10.94.188.18:8000", "GITLAB_TOKEN": "glpat-test"}, clear=False):
                os.environ.pop("GITLAB_PROJECT_ID", None)
                os.environ.pop("CI_PROJECT_ID", None)
                client = GitLabClient(repo_root=repo)
                self.assertEqual(client.project_id, "ht-yangdawei/uw-research-report-adk")
                self.assertTrue(client.enabled)

                # Verify get_git_forge also picks it up
                forge = get_git_forge(repo_root=repo)
                self.assertIsInstance(forge, GitLabClient)
                self.assertEqual(forge.project_id, "ht-yangdawei/uw-research-report-adk")

    def test_codebuddy_worker_cleans_port_and_injects_port_0(self) -> None:
        """run_codebuddy_worker must strip parent PORT from env and add --port 0 to prevent EADDRINUSE."""
        with tempfile.TemporaryDirectory() as td:
            worktree = Path(td)
            with patch.dict(os.environ, {"PORT": "52445", "CODEBUDDY_PORT": "52445"}, clear=False):
                captured_cmd = []
                captured_env = {}

                async def fake_run_subproc(cmd, cwd, env=None, stdin_text=None, timeout_sec=None):
                    nonlocal captured_cmd, captured_env
                    captured_cmd = list(cmd)
                    captured_env = dict(env or {})
                    from adk_dev_flow.subproc import CommandResult
                    return CommandResult(returncode=0, stdout='{"changed_files": [], "summary": "done"}', stderr="", timed_out=False)

                with patch("adk_dev_flow.codebuddy_bridge.run_subprocess_safe", side_effect=fake_run_subproc):
                    import asyncio
                    asyncio.run(
                        run_codebuddy_worker(
                            worktree=worktree,
                            prompt="fix code",
                            run_id="run-port-test",
                            allowed_paths=["src/"],
                            locked_tests=["tests/"],
                        )
                    )

                # Verify PORT and CODEBUDDY_PORT are stripped from worker env
                self.assertNotIn("PORT", captured_env)
                self.assertNotIn("CODEBUDDY_PORT", captured_env)
                # Verify --port 0 is passed in CLI command
                self.assertIn("--port", captured_cmd)
                port_idx = captured_cmd.index("--port")
                self.assertEqual(captured_cmd[port_idx + 1], "0")

    def test_dashboard_finds_available_port_on_conflict(self) -> None:
        """find_available_port must automatically find next available port when start_port is busy."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", 0))
            sock.listen(1)
            busy_port = sock.getsockname()[1]
            # Busy port is now occupied
            avail_port = find_available_port(host="127.0.0.1", start_port=busy_port)
            self.assertNotEqual(avail_port, busy_port)
            self.assertGreater(avail_port, busy_port)
        finally:
            sock.close()

    def test_project_level_config_loader(self) -> None:
        """load_project_config must read .adk/config.json and .adk/config.local.json into env."""
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            adk_dir = repo / ".adk"
            adk_dir.mkdir(parents=True, exist_ok=True)

            config_file = adk_dir / "config.json"
            config_file.write_text(
                json.dumps(
                    {
                        "gitlab_url": "http://gitlab.internal.corp",
                        "gitlab_project_id": "team/my-special-project",
                        "test_cmd": "python -m pytest -q {test_path}",
                        "worker_model": "deepseek-v4.1-flash",
                        "env": {
                            "CUSTOM_PROJECT_VAR": "secret-value",
                        },
                    }
                ),
                encoding="utf-8",
            )

            loaded = load_project_config(repo)
            self.assertEqual(loaded.get("GITLAB_URL"), "http://gitlab.internal.corp")
            self.assertEqual(loaded.get("GITLAB_PROJECT_ID"), "team/my-special-project")
            self.assertEqual(loaded.get("ADK_TEST_CMD"), "python -m pytest -q {test_path}")
            self.assertEqual(loaded.get("ADK_WORKER_MODEL"), "deepseek-v4.1-flash")
            self.assertEqual(loaded.get("CUSTOM_PROJECT_VAR"), "secret-value")

    def test_plugin_builder_preserves_custom_env_vars(self) -> None:
        """build_plugin_bundle must merge and preserve custom env vars in existing mcp_config.json."""
        with tempfile.TemporaryDirectory() as td_repo, tempfile.TemporaryDirectory() as td_target:
            repo = Path(td_repo)
            target = Path(td_target)

            # Pre-create target with custom env vars in mcp_config.json and .mcp.json
            pre_config = {
                "mcpServers": {
                    "adk-dev-flow": {
                        "command": "python",
                        "args": ["-m", "adk_dev_flow.mcp_server"],
                        "env": {
                            "PYTHONUNBUFFERED": "1",
                            "GITLAB_TOKEN": "glpat-pre-existing-token",
                            "ADK_TEST_CMD": "my-custom-python-test",
                        },
                    }
                }
            }
            (target / "mcp_config.json").write_text(json.dumps(pre_config, indent=2), encoding="utf-8")
            (target / ".mcp.json").write_text(json.dumps(pre_config, indent=2), encoding="utf-8")

            # Run build
            build_plugin_bundle(repo, target)

            # Verify custom env vars are preserved and not erased
            updated_mcp = json.loads((target / "mcp_config.json").read_text(encoding="utf-8"))
            server_env = updated_mcp["mcpServers"]["adk-dev-flow"]["env"]
            self.assertEqual(server_env.get("GITLAB_TOKEN"), "glpat-pre-existing-token")
            self.assertEqual(server_env.get("ADK_TEST_CMD"), "my-custom-python-test")
            self.assertEqual(server_env.get("REPO_ROOT"), ".")
            self.assertEqual(server_env.get("PYTHONUNBUFFERED"), "1")

    def test_mcp_service_tracks_run_roots_across_repos(self) -> None:
        """Switching repo_root in flow_init must not lose track of earlier runs in other repos."""
        import asyncio

        with tempfile.TemporaryDirectory() as td1, tempfile.TemporaryDirectory() as td2:
            repo1 = Path(td1)
            repo2 = Path(td2)
            subprocess.run(["git", "init", "-q"], cwd=repo1, check=True)
            subprocess.run(["git", "init", "-q"], cwd=repo2, check=True)

            _, service = create_mcp_server(repo_root=repo1)
            r1 = asyncio.run(service.flow_init("Req 1", repo_root=str(repo1)))["run_id"]
            r2 = asyncio.run(service.flow_init("Req 2", repo_root=str(repo2)))["run_id"]

            st1 = service.flow_status(r1)
            st2 = service.flow_status(r2)
            self.assertNotIn("error", st1, f"Lost track of repo1 run_id: {st1}")
            self.assertNotIn("error", st2)
            self.assertEqual(st1["run_id"], r1)
            self.assertEqual(st2["run_id"], r2)


if __name__ == "__main__":
    unittest.main()
