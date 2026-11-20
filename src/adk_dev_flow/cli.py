from __future__ import annotations

import argparse
from importlib import resources
from pathlib import Path


def _read_template(name: str) -> str:
    return resources.files("adk_dev_flow.templates").joinpath(name).read_text(encoding="utf-8")


def _write_if_needed(target: Path, content: str, *, force: bool) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not force:
        return f"skipped (exists): {target}"
    target.write_text(content, encoding="utf-8")
    return f"written: {target}"


def _ensure_gitignore_entry(repo_root: Path, entry: str = ".adk/") -> str:
    gitignore = repo_root / ".gitignore"
    if gitignore.exists():
        lines = gitignore.read_text(encoding="utf-8").splitlines()
        if any(line.strip() == entry for line in lines):
            return f"skipped (already contains {entry}): {gitignore}"
        prefix = "\n" if lines and lines[-1].strip() != "" else ""
        gitignore.write_text(
            gitignore.read_text(encoding="utf-8") + f"{prefix}\n# ADK dev flow state\n{entry}\n",
            encoding="utf-8",
        )
        return f"updated: {gitignore}"
    gitignore.write_text(f"# ADK dev flow state\n{entry}\n", encoding="utf-8")
    return f"written: {gitignore}"


def init_target_repo(
    target_dir: Path,
    *,
    include_ci: bool = False,
    force: bool = False,
) -> list[str]:
    """Scaffold thin .codebuddy/ configuration templates into a target Python repository."""
    root = target_dir.resolve()
    actions: list[str] = []

    mapping = [
        ("SKILL.md", root / ".codebuddy" / "skills" / "deterministic-dev" / "SKILL.md"),
        ("mcp.json", root / ".codebuddy" / "mcp.json"),
        ("settings.json", root / ".codebuddy" / "settings.json"),
    ]
    if include_ci:
        mapping.append(("gitlab-ci.yml", root / ".gitlab-ci.yml"))

    for tpl_name, dest in mapping:
        actions.append(_write_if_needed(dest, _read_template(tpl_name), force=force))

    actions.append(_ensure_gitignore_entry(root, ".adk/"))
    return actions


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="adk-dev-flow-init",
        description="Scaffold thin .codebuddy/ templates for adk-dev-flow into a target repository.",
    )
    parser.add_argument(
        "target",
        nargs="?",
        default=".",
        help="Target business repository root directory (default: current directory).",
    )
    parser.add_argument(
        "--with-ci",
        action="store_true",
        help="Also generate a starter .gitlab-ci.yml file.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing template files.",
    )
    args = parser.parse_args(argv)
    actions = init_target_repo(Path(args.target), include_ci=args.with_ci, force=args.force)
    for line in actions:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
