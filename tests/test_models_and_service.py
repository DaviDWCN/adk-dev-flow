from __future__ import annotations

import asyncio
import importlib
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from adk_dev_flow.state_store import StateStore


def _has_real_pydantic() -> bool:
    try:
        mod = importlib.import_module("pydantic")
        return hasattr(mod, "ValidationError")
    except ModuleNotFoundError:
        return False


def _install_flow_service_stubs() -> list[str]:
    """Stub fastmcp / google.adk / gitlab / pydantic ONLY when absent from host Python."""
    injected: list[str] = []

    def _add(name: str, mod: types.ModuleType) -> None:
        sys.modules[name] = mod
        injected.append(name)

    if "pydantic" not in sys.modules:
        try:
            importlib.import_module("pydantic")
        except ModuleNotFoundError:
            pyd = types.ModuleType("pydantic")

            class _BaseModel:
                def __init__(self, **kwargs):
                    for k, v in kwargs.items():
                        setattr(self, k, v)

                def model_dump(self):
                    return dict(self.__dict__)

                def model_dump_json(self):
                    return json.dumps(self.model_dump())

                @classmethod
                def model_validate(cls, data):
                    return cls(**data) if isinstance(data, dict) else data

                @classmethod
                def model_validate_json(cls, raw):
                    return cls(**json.loads(raw))

                @classmethod
                def model_json_schema(cls):
                    return {"type": "object", "title": cls.__name__}

            pyd.BaseModel = _BaseModel  # type: ignore[attr-defined]
            pyd.Field = lambda *a, **kw: None  # type: ignore[attr-defined]
            pyd.model_validator = lambda *a, **kw: (lambda fn: fn)  # type: ignore[attr-defined]
            _add("pydantic", pyd)

    if "fastmcp" not in sys.modules:
        try:
            importlib.import_module("fastmcp")
        except ModuleNotFoundError:
            fm = types.ModuleType("fastmcp")

            class _FastMCP:
                def __init__(self, name: str) -> None:
                    self.name = name

                def tool(self):
                    return lambda fn: fn

                def run(self) -> None:
                    pass

            fm.FastMCP = _FastMCP  # type: ignore[attr-defined]
            _add("fastmcp", fm)

    if "gitlab" not in sys.modules:
        try:
            importlib.import_module("gitlab")
        except ModuleNotFoundError:
            _add("gitlab", types.ModuleType("gitlab"))

    if "google.adk" not in sys.modules:
        try:
            importlib.import_module("google.adk")
        except ModuleNotFoundError:
            for name in (
                "google",
                "google.adk",
                "google.adk.models",
                "google.adk.models.llm_request",
                "google.adk.models.llm_response",
                "google.adk.sessions",
                "google.adk.workflow",
                "google.genai",
            ):
                if name not in sys.modules:
                    _add(name, types.ModuleType(name))

            sys.modules["google.adk"].Agent = MagicMock  # type: ignore[attr-defined]
            sys.modules["google.adk"].Event = lambda output=None, route=None: types.SimpleNamespace(output=output, route=route)  # type: ignore[attr-defined]
            sys.modules["google.adk"].Runner = MagicMock  # type: ignore[attr-defined]
            sys.modules["google.adk"].Workflow = MagicMock  # type: ignore[attr-defined]
            sys.modules["google.adk.sessions"].InMemorySessionService = MagicMock  # type: ignore[attr-defined]
            sys.modules["google.adk.workflow"].START = "START"  # type: ignore[attr-defined]
            sys.modules["google.adk.models"].BaseLlm = object  # type: ignore[attr-defined]
            sys.modules["google.adk.models.llm_request"].LlmRequest = object  # type: ignore[attr-defined]
            sys.modules["google.adk.models.llm_response"].LlmResponse = object  # type: ignore[attr-defined]
            sys.modules["google.genai"].types = types.SimpleNamespace(Content=MagicMock, Part=MagicMock)  # type: ignore[attr-defined]

    return injected


@unittest.skipUnless(_has_real_pydantic(), "requires real pydantic package")
class TestSpecContractValidation(unittest.TestCase):
    def test_spec_contract_rejects_syntax_error_and_out_of_whitelist_targets(self) -> None:
        from pydantic import ValidationError

        from adk_dev_flow.models import AtomicTask, SpecContract

        # Broken Python syntax in acceptance test -> rejected at lock time
        with self.assertRaises(ValidationError):
            SpecContract(
                title="Bad syntax",
                summary="demo",
                allowed_paths=["src"],
                acceptance_test_path="tests/test_bad.py",
                acceptance_test_code="def broken(:\n    pass\n",
                tasks=[AtomicTask(id="T1", title="t", description="d", target_files=["src/a.py"])],
            )

        # Target file outside allowed_paths -> rejected
        with self.assertRaises(ValidationError):
            SpecContract(
                title="Out of bounds",
                summary="demo",
                allowed_paths=["src/api"],
                acceptance_test_path="tests/test_ok.py",
                acceptance_test_code="def test_ok(): assert True\n",
                tasks=[
                    AtomicTask(
                        id="T1", title="t", description="d", target_files=["src/api_admin/a.py"]
                    )
                ],
            )

        # Valid contract passes
        spec = SpecContract(
            title="Valid",
            summary="demo",
            allowed_paths=["src/api"],
            acceptance_test_path="tests/test_ok.py",
            acceptance_test_code="def test_ok():\n    assert 1 + 1 == 2\n",
            tasks=[AtomicTask(id="T1", title="t", description="d", target_files=["src/api/a.py"])],
        )
        self.assertEqual(spec.allowed_paths, ["src/api"])


class TestFlowServiceLifecycle(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._injected = _install_flow_service_stubs()
        from adk_dev_flow import mcp_server

        cls.mcp_server = mcp_server

    @classmethod
    def tearDownClass(cls) -> None:
        for name in reversed(cls._injected):
            sys.modules.pop(name, None)

    async def test_init_lock_dispatch_cas_resume_list_and_finalize(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = StateStore(root)
            fake_gl = MagicMock()
            fake_gl.enabled = False

            svc = self.mcp_server.FlowService(root, store, fake_gl)

            # 1) flow_init
            init_res = await svc.flow_init("Add feature X")
            run_id = init_res["run_id"]
            self.assertEqual(init_res["status"], "SPEC_DRAFTING")

            # 2) flow_lock_spec
            fake_spec = MagicMock()
            fake_spec.acceptance_test_path = "tests/test_x.py"
            fake_spec.allowed_paths = ["src"]
            fake_spec.model_dump_json.return_value = json.dumps({"title": "X"})

            lock_res = svc.flow_lock_spec(run_id, fake_spec)
            self.assertEqual(lock_res["status"], "SPEC_LOCKED")
            # Re-locking from SPEC_LOCKED must fail
            self.assertIn("error", svc.flow_lock_spec(run_id, fake_spec))

            # 3) flow_dispatch CAS prevents duplicate runs
            started = asyncio.Event()

            async def _fake_exec(*args, **kwargs):
                started.set()
                await asyncio.sleep(0.05)

            with patch.object(self.mcp_server, "execute_workflow_run", side_effect=_fake_exec):
                d1 = await svc.flow_dispatch(run_id)
                d2 = await svc.flow_dispatch(run_id)
                self.assertEqual(d1["status"], "RUNNING")
                self.assertIn("error", d2)
                await started.wait()
                if run_id in svc.bg_tasks:
                    await svc.bg_tasks[run_id]

            # 4) flow_resume requires ESCALATED + non-empty instruction
            self.assertIn("error", await svc.flow_resume(run_id, "fix it"))
            store.update_run(run_id, status="ESCALATED", attempt=3, last_error_sig="abc")
            self.assertIn("error", await svc.flow_resume(run_id, "   "))

            with patch.object(
                self.mcp_server, "execute_workflow_run", new_callable=AsyncMock
            ) as mock_exec:
                res_resume = await svc.flow_resume(run_id, "Use timezone-aware UTC datetime")
                self.assertEqual(res_resume["status"], "RUNNING")
                if run_id in svc.bg_tasks:
                    await svc.bg_tasks[run_id]
                mock_exec.assert_awaited_once()
                self.assertEqual(
                    mock_exec.call_args.kwargs.get("human_hint"),
                    "Use timezone-aware UTC datetime",
                )

            # 5) flow_list returns the run
            listed = svc.flow_list(limit=5)
            self.assertEqual(listed["count"], 1)
            self.assertEqual(listed["runs"][0]["run_id"], run_id)

            # 6) flow_finalize requires MR_READY
            self.assertIn("error", await svc.flow_finalize(run_id))
            store.update_run(run_id, status="MR_READY")
            fin = await svc.flow_finalize(run_id, remove_worktree=False)
            self.assertEqual(fin["status"], "MR_READY")


if __name__ == "__main__":
    unittest.main()
