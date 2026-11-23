"""Sync the freshly built plugin bundle into the locally installed CodeBuddy plugin.

CodeBuddy caches installed plugins under ``~/.codebuddy/plugins/cache/<marketplace>/<plugin>/<version>``
and ignores source changes when the version is unchanged. After ``git pull``, run this
script to rebuild the bundle from the current checkout and refresh the installed copy
in place (including the recorded git commit SHA). A new CodeBuddy session is required
for skills and MCP settings to take effect.

Usage:
    python scripts/sync_local_plugin.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_ID = "adk-dev-flow@adk-dev-flow-local"
REGISTRY = Path.home() / ".codebuddy" / "plugins" / "installed_plugins.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _head_sha() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def main() -> int:
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from adk_dev_flow.plugin_builder import sync_plugin_all

    # 1. Rebuild the bundle in the repo (and the Gemini global dir) from current source.
    result = sync_plugin_all(REPO_ROOT, install_global=True)
    source = Path(result["repo_plugin"]["target_dir"])

    # 2. Locate the installed CodeBuddy copy.
    if not REGISTRY.is_file():
        print(f"Plugin registry not found: {REGISTRY}", file=sys.stderr)
        return 1
    registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    entries = registry.get("plugins", {}).get(PLUGIN_ID)
    if not entries:
        print(f"Plugin {PLUGIN_ID} is not installed; run: codebuddy plugin install {PLUGIN_ID}", file=sys.stderr)
        return 1
    install = Path(entries[0]["installPath"])
    if not install.is_dir():
        print(f"Install path does not exist: {install}", file=sys.stderr)
        return 1

    # 3. Mirror the bundle into the installed copy, preserving CodeBuddy's .in_use markers.
    source_names = {item.name for item in source.iterdir()}
    for item in install.iterdir():
        if item.name in source_names or item.name.startswith(".in_use"):
            continue
        if item.is_dir():
            shutil.rmtree(item)
        else:
            item.unlink()
    for item in source.iterdir():
        dest = install / item.name
        if item.is_dir():
            shutil.copytree(item, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(item, dest)

    # 4. Refresh installation metadata to reflect the code actually installed.
    entries[0]["gitCommitSha"] = _head_sha()
    entries[0]["lastUpdated"] = _utc_now()
    REGISTRY.write_text(json.dumps(registry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"Bundle:    {source}")
    print(f"Installed: {install}")
    print(f"Commit:    {entries[0]['gitCommitSha']}")
    print(f"Version:   {entries[0]['version']}")
    print("Done. Start a new CodeBuddy session (or /reload-plugins) to load the update.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
