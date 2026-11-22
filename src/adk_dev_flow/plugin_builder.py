from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any


def get_default_gemini_plugins_dir() -> Path:
    """Resolve global ~/.gemini/config/plugins path across platforms."""
    user_home = Path.home()
    return user_home / ".gemini" / "config" / "plugins"


def generate_logo_svg() -> str:
    """Generate a clean, modern SVG logo for adk-dev-flow plugin."""
    return """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256" width="256" height="256">
  <defs>
    <linearGradient id="bgGrad" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#0f172a"/>
      <stop offset="50%" stop-color="#1e293b"/>
      <stop offset="100%" stop-color="#090d16"/>
    </linearGradient>
    <linearGradient id="accentGrad" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#38bdf8"/>
      <stop offset="100%" stop-color="#2563eb"/>
    </linearGradient>
    <linearGradient id="goldGrad" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#fcd34d"/>
      <stop offset="100%" stop-color="#f59e0b"/>
    </linearGradient>
    <filter id="glow" x="-20%" y="-20%" width="140%" height="140%">
      <feGaussianBlur stdDeviation="4" result="blur" />
      <feComposite in="SourceGraphic" in2="blur" operator="over" />
    </filter>
  </defs>

  <!-- Background Base -->
  <rect width="256" height="256" rx="48" fill="url(#bgGrad)" stroke="#334155" stroke-width="4"/>

  <!-- Pipeline Network Track -->
  <path d="M 48 80 L 128 48 L 208 80 L 208 176 L 128 208 L 48 176 Z" 
        fill="none" stroke="#1e3a8a" stroke-width="8" stroke-dasharray="8 8" opacity="0.6"/>

  <!-- Central Shield / Workstation Badge -->
  <path d="M 128 54 L 196 86 L 196 150 C 196 182 166 206 128 218 C 90 206 60 182 60 150 L 60 86 Z" 
        fill="#0f172a" stroke="url(#accentGrad)" stroke-width="5" filter="url(#glow)"/>

  <!-- Code / AST Graph Nodes inside Shield -->
  <circle cx="128" cy="94" r="10" fill="url(#goldGrad)" />
  <circle cx="94" cy="142" r="8" fill="#38bdf8" />
  <circle cx="162" cy="142" r="8" fill="#38bdf8" />
  <circle cx="128" cy="176" r="9" fill="#10b981" />

  <!-- Connection Edges -->
  <line x1="128" y1="94" x2="94" y2="142" stroke="#64748b" stroke-width="3" />
  <line x1="128" y1="94" x2="162" y2="142" stroke="#64748b" stroke-width="3" />
  <line x1="94" y1="142" x2="128" y2="176" stroke="#64748b" stroke-width="3" />
  <line x1="162" y1="142" x2="128" y2="176" stroke="#64748b" stroke-width="3" />

  <!-- Pixel Gear Center -->
  <rect x="123" y="127" width="10" height="10" fill="#e2e8f0" rx="2"/>
  <rect x="120" y="130" width="16" height="4" fill="#e2e8f0"/>
  <rect x="126" y="124" width="4" height="16" fill="#e2e8f0"/>

  <!-- Terminal Bracket Indicators -->
  <path d="M 78 120 L 70 132 L 78 144" fill="none" stroke="#fcd34d" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/>
  <path d="M 178 120 L 186 132 L 178 144" fill="none" stroke="#fcd34d" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/>
</svg>
"""


def strip_yaml_frontmatter(content: str) -> str:
    """Remove YAML frontmatter (--- ... ---) from Markdown documents for rules/AGENTS.md."""
    stripped = content.strip()
    if stripped.startswith("---"):
        parts = stripped.split("---", 2)
        if len(parts) >= 3:
            return parts[2].strip()
    return stripped


def extract_pyproject_version(repo_root: Path) -> str:
    """Extract version string from pyproject.toml."""
    pyproject_path = repo_root / "pyproject.toml"
    if pyproject_path.exists():
        text = pyproject_path.read_text(encoding="utf-8")
        match = re.search(r'version\s*=\s*["\']([^"\']+)["\']', text)
        if match:
            return match.group(1)
    return "0.2.0"


def build_plugin_bundle(repo_root: Path, target_dir: Path) -> dict[str, Any]:
    """Generate or update the full plugin bundle in the specified directory."""
    root = repo_root.resolve()
    target = target_dir.resolve()
    target.mkdir(parents=True, exist_ok=True)

    version = extract_pyproject_version(root)

    # 1. plugin.json
    plugin_json_data = {
        "name": "adk-dev-flow",
        "displayName": "ADK Deterministic Dev Flow",
        "version": version,
        "description": "面向 Python 业务仓库的确定性 AI 研发工作流系统：FastMCP 13 工具、AST 代码图谱影响面分析、TDD 红灯契约门禁、Git Worktree 隔离施工、独立 Critic 盲审与像素工坊实时看板。",
        "logo": "assets/logo.svg",
        "suggestedPrompts": [
          "按照确定性研发流程处理当前的开发需求",
          "使用 AST 代码图谱分析目标代码的影响面与爆炸半径",
          "打开当前任务的 ADK 像素工坊工位动画看板"
        ],
        "author": {
            "name": "David",
            "email": "david@example.com"
        },
        "homepage": "https://github.com/DaviDWCN/adk-dev-flow",
        "repository": "https://github.com/DaviDWCN/adk-dev-flow.git",
        "license": "MIT",
        "keywords": [
            "adk",
            "mcp",
            "workflow",
            "tdd",
            "ast",
            "code-graph",
            "sandbox",
            "codebuddy",
            "antigravity"
        ]
    }
    (target / "plugin.json").write_text(
        json.dumps(plugin_json_data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    # 2. mcp_config.json
    # Portable runner: use uv run with --directory pointing to the local repo root
    # or fallback to direct binary invocation if globally installed.
    repo_posix = root.as_posix()
    mcp_config_data = {
        "mcpServers": {
            "adk-dev-flow": {
                "command": "uv",
                "args": [
                    "run",
                    "--directory",
                    repo_posix,
                    "adk-dev-flow"
                ],
                "env": {
                    "PYTHONUNBUFFERED": "1"
                }
            }
        }
    }
    (target / "mcp_config.json").write_text(
        json.dumps(mcp_config_data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    # 3. assets/logo.svg
    assets_dir = target / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    (assets_dir / "logo.svg").write_text(generate_logo_svg(), encoding="utf-8")

    # 4. skills/deterministic-dev/SKILL.md
    skills_dir = target / "skills" / "deterministic-dev"
    skills_dir.mkdir(parents=True, exist_ok=True)
    src_skill = root / "src" / "adk_dev_flow" / "templates" / "SKILL.md"
    if src_skill.exists():
        shutil.copy2(src_skill, skills_dir / "SKILL.md")
    else:
        # Fallback if path differs
        (skills_dir / "SKILL.md").write_text("# deterministic-dev\n", encoding="utf-8")

    # 5. rules/AGENTS.md (plain markdown without YAML frontmatter)
    rules_dir = target / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    src_rules = root / "src" / "adk_dev_flow" / "templates" / "engineering_rules.md"
    if src_rules.exists():
        raw_rules = src_rules.read_text(encoding="utf-8")
        clean_rules = strip_yaml_frontmatter(raw_rules)
        (rules_dir / "AGENTS.md").write_text(clean_rules + "\n", encoding="utf-8")
    else:
        (rules_dir / "AGENTS.md").write_text("# Engineering Invariants\n", encoding="utf-8")

    # 6. README.md
    readme_content = f"""# ADK Deterministic Dev Flow Plugin (v{version})

面向所有 Agent 的确定性研发流水线插件包。

## 核心特性
- 🛠️ **13 个 FastMCP 原子工具**：包括 `flow_init`, `flow_lock_spec`, `flow_analyze_impact`, `flow_dispatch`, `flow_dashboard` 等。
- 🌲 **AST 代码图谱与影响面分析**：自动推导修改的爆炸半径与测试集。
- 🎨 **像素工坊工位动画看板**：支持 Aux Pane 侧边栏及内联渲染。
- 🛡️ **物理沙箱与 TDD Red 强制契约**：杜绝非白名单越权修改。

## 安装与使用
当此插件启用后，Agent 自动获取 `deterministic-dev` 研发技能与强一致性工程规范。
"""
    (target / "README.md").write_text(readme_content, encoding="utf-8")

    return {
        "target_dir": str(target),
        "version": version,
        "files_generated": [
            "plugin.json",
            "mcp_config.json",
            "assets/logo.svg",
            "skills/deterministic-dev/SKILL.md",
            "rules/AGENTS.md",
            "README.md",
        ],
    }


def sync_plugin_all(repo_root: Path, install_global: bool = True) -> dict[str, Any]:
    """Synchronize plugin files into the repo's plugin/ directory and ~/.gemini/config/plugins/adk-dev-flow."""
    root = repo_root.resolve()
    repo_plugin_dir = root / "plugin" / "adk-dev-flow"
    repo_res = build_plugin_bundle(root, repo_plugin_dir)

    global_res = None
    if install_global:
        global_target = get_default_gemini_plugins_dir() / "adk-dev-flow"
        global_res = build_plugin_bundle(root, global_target)

    return {
        "repo_plugin": repo_res,
        "global_plugin": global_res,
    }


def cli_main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="adk-dev-flow-sync-plugin",
        description="Build and synchronize adk-dev-flow plugin to repo and global ~/.gemini/config/plugins.",
    )
    parser.add_argument(
        "--repo-root",
        default=".",
        help="Repository root directory (default: current directory).",
    )
    parser.add_argument(
        "--no-global",
        action="store_true",
        help="Do not synchronize to global ~/.gemini/config/plugins directory.",
    )
    args = parser.parse_args(argv)

    root = Path(args.repo_root).resolve()
    res = sync_plugin_all(root, install_global=not args.no_global)

    print("=" * 60)
    print("🔌 ADK Dev Flow Plugin 同步构建完成")
    print("=" * 60)
    print(f"📦 仓库内置插件路径: {res['repo_plugin']['target_dir']}")
    if res['global_plugin']:
        print(f"🌐 全局 Agent 插件路径: {res['global_plugin']['target_dir']}")
    print(f"🏷️  版本: v{res['repo_plugin']['version']}")
    print("📑 生成组件:")
    for f in res['repo_plugin']['files_generated']:
        print(f"  - {f}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(cli_main())
