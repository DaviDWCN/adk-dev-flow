from __future__ import annotations

import asyncio
import os
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


async def _terminate_process_tree(proc: asyncio.subprocess.Process) -> None:
    """Forcefully terminate a subprocess and its process group to prevent orphan leaks."""
    if proc.returncode is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except ProcessLookupError:
        pass
    except Exception:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    try:
        await proc.wait()
    except Exception:
        pass


async def run_subprocess_safe(
    cmd: list[str],
    *,
    cwd: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    timeout_sec: float = 120.0,
    stdin_text: str | None = None,
) -> CommandResult:
    """Run an async subprocess with timeout & guaranteed process-tree cleanup on timeout/cancel."""
    kwargs: dict[str, object] = {
        "cwd": str(cwd) if cwd is not None else None,
        "env": dict(env) if env is not None else None,
        "stdin": asyncio.subprocess.PIPE if stdin_text is not None else asyncio.subprocess.DEVNULL,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
    }
    if os.name == "posix":
        kwargs["start_new_session"] = True

    proc = await asyncio.create_subprocess_exec(*cmd, **kwargs)  # type: ignore[arg-type]
    stdin_bytes = stdin_text.encode("utf-8") if stdin_text is not None else None

    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(input=stdin_bytes),
            timeout=timeout_sec,
        )
        return CommandResult(
            returncode=proc.returncode if proc.returncode is not None else -1,
            stdout=stdout_b.decode("utf-8", errors="replace"),
            stderr=stderr_b.decode("utf-8", errors="replace"),
            timed_out=False,
        )
    except asyncio.TimeoutError:
        await _terminate_process_tree(proc)
        return CommandResult(
            returncode=-1,
            stdout="",
            stderr=f"Command timed out after {timeout_sec}s: {' '.join(cmd[:4])}",
            timed_out=True,
        )
    except asyncio.CancelledError:
        await _terminate_process_tree(proc)
        raise
