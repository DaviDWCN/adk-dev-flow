from __future__ import annotations

import asyncio
import json
import os
import shutil
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from google.adk.models import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types


def _extract_cli_text(raw_stdout: str) -> str:
    raw = raw_stdout.strip()
    if not raw:
        return ""
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            if "structured_output" in data and data["structured_output"] is not None:
                return json.dumps(data["structured_output"], ensure_ascii=False)
            if "result" in data and data["result"] is not None:
                val = data["result"]
                return val if isinstance(val, str) else json.dumps(val, ensure_ascii=False)
        return raw
    except json.JSONDecodeError:
        return raw


class CodeBuddyLlm(BaseLlm):
    """将本机 CodeBuddy CLI 桥接为 Google ADK 的 BaseLlm 模型源（零额外 API Key）。"""

    model: str = "codebuddy-cli"
    cli_bin: str = "codebuddy"
    timeout_sec: int = 180

    @classmethod
    def supported_models(cls) -> list[str]:
        return [r"codebuddy.*"]

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        sections: list[str] = []
        if llm_request.config and llm_request.config.system_instruction:
            sys_inst = llm_request.config.system_instruction
            if isinstance(sys_inst, str):
                sections.append(f"[SYSTEM]\n{sys_inst}")
            elif hasattr(sys_inst, "parts"):
                txt = "\n".join(p.text for p in sys_inst.parts if getattr(p, "text", None))
                if txt:
                    sections.append(f"[SYSTEM]\n{txt}")

        for content in llm_request.contents or []:
            role = (content.role or "user").upper()
            parts_txt = "\n".join(
                p.text for p in (content.parts or []) if getattr(p, "text", None)
            )
            if parts_txt:
                sections.append(f"[{role}]\n{parts_txt}")

        prompt = "\n\n".join(sections)
        cmd = [
            shutil.which(self.cli_bin) or self.cli_bin,
            "-p",
            prompt,
            "--output-format",
            "json",
            "--no-session-persistence",
            "--max-turns",
            "1",
            "--allowedTools",
            "",
        ]

        if llm_request.config and getattr(llm_request.config, "response_schema", None):
            schema_obj = llm_request.config.response_schema
            if hasattr(schema_obj, "model_dump"):
                schema_json = json.dumps(
                    schema_obj.model_dump(exclude_none=True), ensure_ascii=False
                )
                cmd.extend(["--json-schema", schema_json])

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout_sec)
        if proc.returncode != 0:
            err_msg = stderr.decode(errors="ignore")
            raise RuntimeError(f"CodeBuddyLlm 调用失败 (exit={proc.returncode}): {err_msg}")

        text = _extract_cli_text(stdout.decode(errors="ignore"))
        yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text=text)]))


async def run_codebuddy_worker(
    worktree: Path,
    prompt: str,
    run_id: str,
    allowed_paths: list[str],
    locked_tests: list[str],
    max_turns: int = 15,
    timeout_sec: int = 300,
) -> dict[str, Any]:
    """在隔离 Git Worktree 中拉起全新、干净上下文的 Headless CodeBuddy 工兵。"""
    schema = json.dumps(
        {
            "type": "object",
            "properties": {
                "changed_files": {"type": "array", "items": {"type": "string"}},
                "summary": {"type": "string"},
            },
            "required": ["changed_files", "summary"],
        }
    )
    env = os.environ.copy()
    env["ADK_WORKER_TOKEN"] = run_id
    env["ADK_ALLOWED_PATHS"] = json.dumps(allowed_paths, ensure_ascii=False)
    env["ADK_LOCKED_TESTS"] = json.dumps(locked_tests, ensure_ascii=False)

    cmd = [
        shutil.which("codebuddy") or "codebuddy",
        "-p",
        prompt,
        "--output-format",
        "json",
        "--json-schema",
        schema,
        "--no-session-persistence",
        "--max-turns",
        str(max_turns),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(worktree),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
    if proc.returncode != 0:
        err_msg = stderr.decode(errors="ignore")[:1000]
        raise RuntimeError(f"CodeBuddy Worker 异常退出 ({proc.returncode}): {err_msg}")

    text = _extract_cli_text(stdout.decode(errors="ignore"))
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"changed_files": [], "summary": text[:500]}
