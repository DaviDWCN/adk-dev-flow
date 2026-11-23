from __future__ import annotations

import json
import os
import re
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


def _extract_json_substring(text: str) -> str:
    cleaned = strip_markdown_code_fence(text.strip())
    try:
        json.loads(cleaned)
        return cleaned
    except json.JSONDecodeError:
        pass

    m = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", cleaned, re.IGNORECASE)
    if m:
        try:
            candidate = m.group(1).strip()
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end > start:
        candidate = cleaned[start : end + 1].strip()
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            pass

    return cleaned


def extract_cli_text(raw_stdout: str) -> str:
    """Extract structured_output or result text from CodeBuddy CLI JSON output."""
    raw = raw_stdout.strip()
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return _extract_json_substring(raw)

    if isinstance(data, list) and data:
        for item in reversed(data):
            if isinstance(item, dict) and item.get("type") == "result":
                if item.get("result") is not None:
                    val = item["result"]
                    return _extract_json_substring(val) if isinstance(val, str) else json.dumps(val, ensure_ascii=False)
            elif isinstance(item, dict) and item.get("type") == "message" and item.get("role") == "assistant":
                for part in item.get("content", []):
                    if isinstance(part, dict) and part.get("type") == "output_text":
                        return _extract_json_substring(part.get("text", ""))

    if isinstance(data, dict):
        if data.get("structured_output") is not None:
            val = data["structured_output"]
            return _extract_json_substring(val) if isinstance(val, str) else json.dumps(val, ensure_ascii=False)
        if data.get("result") is not None:
            val = data["result"]
            if isinstance(val, str):
                return _extract_json_substring(val)
            return json.dumps(val, ensure_ascii=False)
    return _extract_json_substring(raw)


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
        system_instruction_text = ""
        if llm_request.config and llm_request.config.system_instruction:
            sys_inst = llm_request.config.system_instruction
            if isinstance(sys_inst, str):
                system_instruction_text = sys_inst
            elif hasattr(sys_inst, "parts"):
                system_instruction_text = "\n".join(
                    p.text for p in sys_inst.parts if getattr(p, "text", None)
                )

        user_content_parts: list[str] = []
        for content in llm_request.contents or []:
            parts_txt = "\n".join(
                p.text for p in (content.parts or []) if getattr(p, "text", None)
            )
            if parts_txt:
                user_content_parts.append(parts_txt)

        prompt_body = "\n\n".join(user_content_parts) or "Review the code changes."

        has_schema = bool(
            llm_request.config and getattr(llm_request.config, "response_schema", None)
        )
        schema_prompt = ""
        if has_schema:
            json_schema = schema_to_json_schema(llm_request.config.response_schema)
            if json_schema is not None:
                schema_prompt = (
                    "\n\n【输出格式要求】请严格只输出一个符合以下 JSON 结构的纯 JSON 对象，不得包含任何 Markdown 标记或额外解释：\n"
                    f"{json.dumps(json_schema, ensure_ascii=False)}"
                )

        full_prompt = (
            f"【审查规约】\n{system_instruction_text}\n\n" if system_instruction_text else ""
        ) + f"【输入上下文】\n{prompt_body}{schema_prompt}"

        binary_name = self.cli_bin or os.environ.get("CODEBUDDY_BIN", "codebuddy")
        resolved_bin = shutil.which(binary_name) or binary_name

        cmd = [
            resolved_bin,
            "-y",
            "--permission-mode",
            "bypassPermissions",
            "--tools",
            "",
            "-p",
            "--max-turns",
            "1",
        ]
        model_name = (
            self.model
            if (self.model and self.model not in ("codebuddy-cli", "codebuddy"))
            else (os.environ.get("ADK_CRITIC_MODEL") or os.environ.get("CODEBUDDY_MODEL"))
        )
        if model_name and model_name not in ("codebuddy-cli", "codebuddy"):
            cmd.extend(["--model", model_name])

        res = await run_subprocess_safe(
            cmd,
            timeout_sec=float(self.timeout_sec),
            stdin_text=full_prompt,
        )
        if res.timed_out:
            raise RuntimeError(f"CodeBuddyLlm 调用超时 ({self.timeout_sec}s): {res.stderr}")
        if res.returncode != 0:
            raise RuntimeError(
                f"CodeBuddyLlm 调用失败 (exit={res.returncode}): {res.stderr[:1000]}"
            )

        text = _extract_json_substring(res.stdout) if has_schema else res.stdout
        if has_schema:
            try:
                json.loads(text)
            except Exception:
                text = json.dumps(
                    {
                        "approved": False,
                        "blocking_issues": [
                            f"模型未按 JSON Schema 格式输出有效 JSON 结论: {text[:200]}"
                        ],
                        "summary": text[:400] or "Critic 输出无法解析为 JSON",
                    },
                    ensure_ascii=False,
                )

        yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text=text)]))


_MODEL_NAME_RE = re.compile(r"^[a-zA-Z0-9_.:/@-]+$")


def validate_model_name(name: str | None) -> str | None:
    """Validate and sanitize model name.

    Returns None for default CLI models.
    Raises ValueError for names containing invalid/dangerous characters.
    """
    if not name:
        return None
    trimmed = name.strip()
    if not trimmed or trimmed in ("codebuddy-cli", "codebuddy", "default"):
        return None
    if not _MODEL_NAME_RE.match(trimmed):
        raise ValueError(f"非法的 Worker 模型名称: {name!r}（包含非法字符或命令注入模式）")
    return trimmed


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
    env["CODEBUDDY_IS_SANDBOX"] = "1"
    # Prevent EADDRINUSE by unsetting inherited parent listening ports
    env.pop("PORT", None)
    env.pop("CODEBUDDY_PORT", None)

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
    cmd = [
        resolved_bin,
        "-y",
        "--permission-mode",
        "bypassPermissions",
    ]
    valid_model = validate_model_name(worker_model)
    if valid_model:
        cmd.extend(["--model", valid_model])

    has_port_arg = any(
        arg == "--port" or arg.startswith("--port=") for arg in extra_args
    )
    if not has_port_arg:
        cmd.extend(["--port", "0"])

    cmd.extend(
        [
            "--output-format",
            "json",
            "--no-session-persistence",
            "--max-turns",
            str(max_turns),
            *extra_args,
        ]
    )

    if use_stdin:
        cmd.append("-p")
    else:
        cmd.extend(["-p", prompt])

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
