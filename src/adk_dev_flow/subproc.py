from __future__ import annotations

import asyncio
import os
import signal
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

if sys.platform == "win32":
    import ctypes
    import subprocess as _subprocess
    from ctypes import wintypes

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
            ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryLimit", ctypes.c_size_t),
            ("PeakJobMemoryLimit", ctypes.c_size_t),
        ]

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    JobObjectExtendedLimitInformation = 9


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


def _assign_process_to_new_job_object(pid: int) -> int | None:
    if sys.platform != "win32":
        return None
    try:
        kernel32 = ctypes.windll.kernel32
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None

        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        res = kernel32.SetInformationJobObject(
            job,
            JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not res:
            kernel32.CloseHandle(job)
            return None

        PROCESS_ALL_ACCESS = 0x1F0FFF
        h_proc = kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)
        if h_proc:
            kernel32.AssignProcessToJobObject(job, h_proc)
            kernel32.CloseHandle(h_proc)
            return job
        else:
            kernel32.CloseHandle(job)
            return None
    except Exception:
        return None


async def _terminate_process_tree(proc: asyncio.subprocess.Process, job_handle: int | None = None) -> None:
    """Forcefully terminate a subprocess and its process group to prevent orphan leaks."""
    if proc.returncode is not None:
        if job_handle and sys.platform == "win32":
            ctypes.windll.kernel32.CloseHandle(job_handle)
        return

    if sys.platform == "win32":
        # 1. First taskkill the process tree while parent PID is registered in OS process table
        try:
            import subprocess as _sync_sp
            _sync_sp.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=_sync_sp.DEVNULL,
                stderr=_sync_sp.DEVNULL,
                timeout=5.0,
            )
        except Exception:
            pass

        # 2. Terminate and close kernel Job Object
        if job_handle:
            ctypes.windll.kernel32.TerminateJobObject(job_handle, 1)
            ctypes.windll.kernel32.CloseHandle(job_handle)
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    try:
        await proc.wait()
    except Exception:
        pass


def no_window_kwargs() -> dict[str, int]:
    """Extra subprocess kwargs that suppress console windows on Windows.

    Raw subprocess.run/Popen call sites that bypass run_subprocess_safe must
    spread these kwargs: a console-subsystem child (git, pytest, node) spawned
    from a windowless parent (pythonw service) would otherwise flash a cmd
    window per call.
    """
    if sys.platform == "win32":
        return {"creationflags": _subprocess.CREATE_NO_WINDOW}
    return {}


def run_subprocess_sync(
    cmd: list[str],
    *,
    cwd: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    timeout_sec: float = 60.0,
) -> CommandResult:
    """Synchronous subprocess helper with Windows console suppression."""
    import subprocess as _sync_sp

    try:
        proc = _sync_sp.run(  # noqa: adk-ast
            cmd,
            cwd=str(cwd) if cwd is not None else None,
            env=dict(env) if env is not None else None,
            stdout=_sync_sp.PIPE,
            stderr=_sync_sp.PIPE,
            text=True,
            timeout=timeout_sec,
            **no_window_kwargs(),
        )
        return CommandResult(
            returncode=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            timed_out=False,
        )
    except _sync_sp.TimeoutExpired:
        return CommandResult(
            returncode=-1,
            stdout="",
            stderr=f"Command timed out after {timeout_sec}s: {' '.join(cmd[:4])}",
            timed_out=True,
        )


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
    elif sys.platform == "win32":
        # Suppress the console window that cmd.exe shims would otherwise open.
        kwargs["creationflags"] = _subprocess.CREATE_NO_WINDOW

    proc = await asyncio.create_subprocess_exec(*cmd, **kwargs)  # type: ignore[arg-type]
    job_handle = _assign_process_to_new_job_object(proc.pid) if sys.platform == "win32" else None
    stdin_bytes = stdin_text.encode("utf-8") if stdin_text is not None else None

    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(input=stdin_bytes),
            timeout=timeout_sec,
        )
        if job_handle and sys.platform == "win32":
            ctypes.windll.kernel32.CloseHandle(job_handle)
        return CommandResult(
            returncode=proc.returncode if proc.returncode is not None else -1,
            stdout=stdout_b.decode("utf-8", errors="replace"),
            stderr=stderr_b.decode("utf-8", errors="replace"),
            timed_out=False,
        )
    except asyncio.TimeoutError:
        await _terminate_process_tree(proc, job_handle)
        return CommandResult(
            returncode=-1,
            stdout="",
            stderr=f"Command timed out after {timeout_sec}s: {' '.join(cmd[:4])}",
            timed_out=True,
        )
    except asyncio.CancelledError:
        await _terminate_process_tree(proc, job_handle)
        raise
