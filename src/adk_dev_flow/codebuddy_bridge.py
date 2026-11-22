from __future__ import annotations

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
from pydantic import BaseModel

from adk_dev_flow.subproc import run_subprocess_safe
from adk_dev_flow.worktree_utils import strip_markdown_code_fence

_GEMINI_TYPE_MAP: dict[str, str] = {
    "OBJECT": "object",
    "ARRAY": "array",
    "STRING": "string",
    "INTEGER": "integer",
    "NUMBER": "number",
    "BOOLEAN": "boolean",
    "NULL": "null",
}


def _normalize_schema_node(node: Any) -> Any:
    """Recursively convert Gemini Schema uppercase types into standard JSON Schema."""
    if isinstance(node, list):
        return [_normalize_schema_node(item) for item in node]
    if not isinstance(node, dict):
        return node

    out: dict[str, Any] = {}
    nullable = False
    for k, v in node.items():
        if v is None:
            continue
        if k == "nullable":
            nullable = bool(v)
            continue
        if k == "type" and isinstance(v, str):
            out[k] = _GEMINI_TYPE_MAP.get(v.upper(), v.lower())
        elif k == "properties" and isinstance(v, dict):
            out[k] = {prop_k: _normalize_schema_node(prop_v) for prop_k, prop_v in v.items()}
        elif k in ("items", "additionalProperties", "additional_properties"):
            target_key = "additionalProperties" if k == "additional_properties" else k
            out[target_key] = _normalize_schema_node(v)
        elif k in ("any_of", "anyOf"):
            out["anyOf"] = _normalize_schema_node(v)
        else:
            out[k] = _normalize_schema_node(v)

    if nullable and "type" in out and isinstance(out["type"], str):
        out["type"] = [out["type"], "null"]
    return out


def schema_to_json_schema(schema_obj: Any) -> dict[str, Any] | None:
    """Convert Pydantic BaseModel class/instance, dict, or google.genai.types.Schema to JSON Schema."""
    if schema_obj is None:
        return None
    if isinstance(schema_obj, type) and issubclass(schema_obj, BaseModel):
        return schema_obj.model_json_schema()
    if isinstance(schema_obj, dict):
        return _normalize_schema_node(schema_obj)
    if hasattr(schema_obj, "model_json_schema") and isinstance(schema_obj, type):
        return schema_obj.model_json_schema()
    if hasattr(schema_obj, "model_dump") and callable(schema_obj.model_dump):
        # Instance of google.genai.types.Schema or BaseModel
        raw = schema_obj.model_dump(exclude_none=True, mode="json")
        if isinstance(raw, dict):
            return _normalize_schema_node(raw)
    return None


def extract_cli_text(raw_stdout: str) -> str:
    """Extract structured_output or result text from CodeBuddy CLI JSON output."""
    raw = raw_stdout.strip()
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return strip_markdown_code_fence(raw)

    if isinstance(data, dict):
        if data.get("structured_output") is not None:
            val = data["structured_output"]
            return val if isinstance(val, str) else json.dumps(val, ensure_ascii=False)
        if data.get("result") is not None:
            val = data["result"]
            if isinstance(val, str):
                return strip_markdown_code_fence(val)
            return json.dumps(val, ensure_ascii=False)
    return raw


def _parse_json_list_env(var_name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(var_name)
    if raw is None or not raw.strip():
        return list(default)
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(x) for x in parsed]
    except json.JSONDecodeError:
        pass
    return list(default)


def _use_stdin_prompt() -> bool:
    return os.environ.get("CODEBUDDY_USE_STDIN", "false").strip().lower() in ("1", "true", "yes")


class CodeBuddyLlm(BaseLlm):
    """Bridge local CodeBuddy CLI as a Google ADK BaseLlm provider (zero extra API keys)."""

    model: str = "codebuddy-cli"
    cli_bin: str = ""
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
        binary_name = self.cli_bin or os.environ.get("CODEBUDDY_BIN", "codebuddy")
        resolved_bin = shutil.which(binary_name) or binary_name

        # Configurable tool-disabling args to tolerate CodeBuddy CLI flag differences across versions.
        disable_tools_args = _parse_json_list_env(
            "CODEBUDDY_DISABLE_TOOLS_ARGS", ["--tools", ""]
        )
        use_stdin = _use_stdin_prompt()

        cmd = [resolved_bin]
        model_name = (
            self.model
            if (self.model and self.model not in ("codebuddy-cli", "codebuddy"))
            else (os.environ.get("ADK_CRITIC_MODEL") or os.environ.get("CODEBUDDY_MODEL"))
        )
        if model_name and model_name not in ("codebuddy-cli", "codebuddy"):
            cmd.extend(["--model", model_name])

        if use_stdin:
            cmd.append("-p")
        else:
            cmd.extend(["-p", prompt])

        cmd.extend(
            [
                "--output-format",
                "json",
                "--no-session-persistence",
                "--max-turns",
                "1",
                *disable_tools_args,
            ]
        )

        if llm_request.config and getattr(llm_request.config, "response_schema", None):
            json_schema = schema_to_json_schema(llm_request.config.response_schema)
            if json_schema is not None:
                cmd.extend(["--json-schema", json.dumps(json_schema, ensure_ascii=False)])

        res = await run_subprocess_safe(
            cmd,
            timeout_sec=float(self.timeout_sec),
            stdin_text=prompt if use_stdin else None,
        )
        if res.timed_out:
            raise RuntimeError(f"CodeBuddyLlm 调用超时 ({self.timeout_sec}s): {res.stderr}")
        if res.returncode != 0:
            raise RuntimeError(
                f"CodeBuddyLlm 调用失败 (exit={res.returncode}): {res.stderr[:1000]}"
            )

        text = extract_cli_text(res.stdout)
        yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text=text)]))


async def run_codebuddy_worker(
    worktree: Path,
    prompt: str,
    run_id: str,
    allowed_paths: list[str],
    locked_tests: list[str],
    max_turns: int = 15,
    timeout_sec: int = 300,
    model: str | None = None,
) -> dict[str, Any]:
    """Spawn an isolated, stateless Headless CodeBuddy worker inside a Git Worktree."""
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

    binary_name = os.environ.get("CODEBUDDY_BIN", "codebuddy")
    resolved_bin = shutil.which(binary_name) or binary_name
    extra_args = _parse_json_list_env("CODEBUDDY_WORKER_EXTRA_ARGS", [])
    use_stdin = _use_stdin_prompt()

    worker_model = (
        model
        or os.environ.get("ADK_WORKER_MODEL")
        or os.environ.get("CODEBUDDY_WORKER_MODEL")
        or os.environ.get("CODEBUDDY_MODEL")
    )
    cmd = [resolved_bin]
    if worker_model and worker_model not in ("codebuddy-cli", "codebuddy"):
        cmd.extend(["--model", worker_model])

    if use_stdin:
        cmd.append("-p")
    else:
        cmd.extend(["-p", prompt])

    cmd.extend(
        [
            "--output-format",
            "json",
            "--json-schema",
            schema,
            "--no-session-persistence",
            "--max-turns",
            str(max_turns),
            *extra_args,
        ]
    )

    res = await run_subprocess_safe(
        cmd,
        cwd=worktree,
        env=env,
        timeout_sec=float(timeout_sec),
        stdin_text=prompt if use_stdin else None,
    )
    if res.timed_out:
        raise RuntimeError(f"CodeBuddy Worker 执行超时 ({timeout_sec}s): {res.stderr}")
    if res.returncode != 0:
        raise RuntimeError(
            f"CodeBuddy Worker 异常退出 (exit={res.returncode}): {res.stderr[:1000]}"
        )

    text = extract_cli_text(res.stdout)
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
        return {"changed_files": [], "summary": str(parsed)[:500]}
    except json.JSONDecodeError:
        return {"changed_files": [], "summary": text[:500]}
