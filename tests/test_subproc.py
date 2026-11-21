from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

from adk_dev_flow.subproc import run_subprocess_safe


class TestSubproc(unittest.IsolatedAsyncioTestCase):
    async def test_basic_execution_and_stdin(self) -> None:
        res = await run_subprocess_safe(
            [sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"],
            stdin_text="hello adk",
            timeout_sec=5.0,
        )
        self.assertEqual(res.returncode, 0)
        self.assertFalse(res.timed_out)
        self.assertEqual(res.stdout.strip(), "HELLO ADK")

    async def test_timeout_kills_process_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "child.pid"
            script = (
                "import os, subprocess, sys, time; "
                "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
                f"open({str(pid_file)!r}, 'w').write(str(p.pid)); "
                "time.sleep(30)"
            )
            res = await run_subprocess_safe([sys.executable, "-c", script], timeout_sec=0.4)
            self.assertTrue(res.timed_out)
            self.assertEqual(res.returncode, -1)

            if pid_file.exists():
                child_pid = int(pid_file.read_text().strip())
                if sys.platform == "win32":
                    import ctypes
                    from ctypes import wintypes
                    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
                    STILL_ACTIVE = 259
                    terminated = False
                    for _ in range(20):
                        await asyncio.sleep(0.1)
                        h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, child_pid)
                        if not h:
                            terminated = True
                            break
                        code = wintypes.DWORD()
                        ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
                        ctypes.windll.kernel32.CloseHandle(h)
                        if code.value != STILL_ACTIVE:
                            terminated = True
                            break
                    self.assertTrue(terminated, "Child process should be terminated")
                else:
                    await asyncio.sleep(0.1)
                    with self.assertRaises(ProcessLookupError):
                        os.kill(child_pid, 0)

    async def test_cancel_kills_subprocess_and_reraises(self) -> None:
        task = asyncio.create_task(
            run_subprocess_safe([sys.executable, "-c", "import time; time.sleep(30)"], timeout_sec=30)
        )
        await asyncio.sleep(0.15)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task


if __name__ == "__main__":
    unittest.main()
