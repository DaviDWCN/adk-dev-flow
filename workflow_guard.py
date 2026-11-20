from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _block(reason: str) -> None:
    print(f"[ADK Workflow Guard 拦截] {reason}", file=sys.stderr)
    sys.exit(2)


def main() -> None:
    raw = sys.stdin.read().strip()
    if not raw:
        sys.exit(0)
    event = json.loads(raw)
    tool_input = event.get("tool_input") or {}
    file_path = str(tool_input.get("file_path") or tool_input.get("path") or "")
    if not file_path:
        sys.exit(0)

    rel = Path(file_path).as_posix().lstrip("./")
    protected = (".gitlab-ci.yml", ".codebuddy/hooks/", ".codebuddy/settings.json")
    if any(rel.startswith(p) or rel == p for p in protected):
        _block(f"禁止修改受保护的门禁文件: {rel}")

    worker_token = os.environ.get("ADK_WORKER_TOKEN")
    if worker_token:
        locked = json.loads(os.environ.get("ADK_LOCKED_TESTS", "[]"))
        if any(rel == t or rel.endswith(f"/{t}") for t in locked):
            _block(f"禁止篡改 Stage 1 锁定的验收测试文件: {rel}")

        allowed = json.loads(os.environ.get("ADK_ALLOWED_PATHS", "[]"))
        if allowed and not any(rel.startswith(a.rstrip("/")) for a in allowed):
            _block(f"路径 `{rel}` 超出当前 Spec 白名单 {allowed}")
        sys.exit(0)

    marker = Path(".adk/active_run.json")
    if marker.exists():
        _block(
            "当前处于 ADK 受控研发流中，严禁在主工作区直接修改代码；"
            "请通过 adk-dev-flow MCP 推进或调用 flow_abort 退出。"
        )


if __name__ == "__main__":
    main()
