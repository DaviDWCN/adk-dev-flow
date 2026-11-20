from __future__ import annotations

import importlib
import os
import sys
import types
import unittest
from unittest.mock import patch


def _ensure_stub_deps() -> list[str]:
    """Install lightweight stubs only when optional runtime deps aren't installed in test env."""
    injected: list[str] = []

    if "pydantic" not in sys.modules:
        try:
            importlib.import_module("pydantic")
        except ModuleNotFoundError:
            pyd = types.ModuleType("pydantic")

            class _FakeBaseModel:
                @classmethod
                def model_json_schema(cls) -> dict:
                    return {"type": "object", "title": cls.__name__}

            pyd.BaseModel = _FakeBaseModel  # type: ignore[attr-defined]
            sys.modules["pydantic"] = pyd
            injected.append("pydantic")

    if "google.adk.models" not in sys.modules:
        try:
            importlib.import_module("google.adk.models")
        except ModuleNotFoundError:
            for mod_name in (
                "google",
                "google.adk",
                "google.adk.models",
                "google.adk.models.llm_request",
                "google.adk.models.llm_response",
                "google.genai",
            ):
                if mod_name not in sys.modules:
                    sys.modules[mod_name] = types.ModuleType(mod_name)
                    injected.append(mod_name)

            class _FakeBaseLlm:
                pass

            sys.modules["google.adk.models"].BaseLlm = _FakeBaseLlm  # type: ignore[attr-defined]
            sys.modules["google.adk.models.llm_request"].LlmRequest = object  # type: ignore[attr-defined]
            sys.modules["google.adk.models.llm_response"].LlmResponse = object  # type: ignore[attr-defined]
            sys.modules["google.genai"].types = types.SimpleNamespace()  # type: ignore[attr-defined]

    if "gitlab" not in sys.modules:
        try:
            importlib.import_module("gitlab")
        except ModuleNotFoundError:
            sys.modules["gitlab"] = types.ModuleType("gitlab")
            injected.append("gitlab")

    return injected


class TestCodeBuddyBridgeHelpers(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._injected = _ensure_stub_deps()
        from adk_dev_flow import codebuddy_bridge, gitlab_client

        cls.bridge = codebuddy_bridge
        cls.gitlab_mod = gitlab_client

    @classmethod
    def tearDownClass(cls) -> None:
        for name in reversed(cls._injected):
            sys.modules.pop(name, None)

    def test_schema_to_json_schema_handles_model_class_and_gemini_uppercase_schema(self) -> None:
        from pydantic import BaseModel

        # 1) Pydantic BaseModel subclass (must NOT call instance method model_dump()!)
        class DummyVerdict(BaseModel):
            pass

        cls_schema = self.bridge.schema_to_json_schema(DummyVerdict)
        self.assertIsInstance(cls_schema, dict)
        self.assertEqual(cls_schema["type"], "object")  # type: ignore[index]

        # 2) Gemini Schema-like instance returning uppercase enum strings
        class FakeGeminiSchema:
            def model_dump(self, exclude_none: bool = True, mode: str = "json") -> dict:
                return {
                    "type": "OBJECT",
                    "properties": {
                        "approved": {"type": "BOOLEAN"},
                        "blocking_issues": {"type": "ARRAY", "items": {"type": "STRING"}},
                        "note": {"type": "STRING", "nullable": True},
                    },
                    "required": ["approved"],
                }

        normalized = self.bridge.schema_to_json_schema(FakeGeminiSchema())
        self.assertEqual(normalized["type"], "object")  # type: ignore[index]
        self.assertEqual(normalized["properties"]["approved"]["type"], "boolean")  # type: ignore[index]
        self.assertEqual(
            normalized["properties"]["blocking_issues"]["items"]["type"], "string"  # type: ignore[index]
        )
        self.assertEqual(normalized["properties"]["note"]["type"], ["string", "null"])  # type: ignore[index]

    def test_extract_cli_text_variants(self) -> None:
        self.assertEqual(
            self.bridge.extract_cli_text('{"structured_output": {"approved": true}}'),
            '{"approved": true}',
        )
        self.assertEqual(
            self.bridge.extract_cli_text('{"result": "```json\\n{\\"ok\\": 1}\\n```"}'),
            '{"ok": 1}',
        )
        self.assertEqual(self.bridge.extract_cli_text("plain text"), "plain text")

    def test_gitlab_ca_bundle_and_strip_draft_prefix(self) -> None:
        with patch.dict(
            os.environ,
            {
                "GITLAB_URL": "https://git.internal",
                "GITLAB_TOKEN": "tok",
                "GITLAB_PROJECT_ID": "42",
                "GITLAB_CA_BUNDLE": "/etc/ssl/corp-ca.pem",
                "GITLAB_SSL_VERIFY": "false",
            },
            clear=False,
        ):
            client = self.gitlab_mod.GitLabClient()
            self.assertTrue(client.enabled)
            # CA bundle takes precedence over boolean flag
            self.assertEqual(client.ssl_verify, "/etc/ssl/corp-ca.pem")

        with patch.dict(os.environ, {}, clear=True):
            client_default = self.gitlab_mod.GitLabClient()
            self.assertFalse(client_default.enabled)
            # Secure default: TLS verification enabled
            self.assertTrue(client_default.ssl_verify)

        self.assertEqual(self.gitlab_mod.strip_draft_prefix("Draft: Fix bug"), "Fix bug")
        self.assertEqual(self.gitlab_mod.strip_draft_prefix("draft:Fix bug"), "Fix bug")
        self.assertEqual(self.gitlab_mod.strip_draft_prefix("WIP: Add feature"), "Add feature")
        self.assertEqual(self.gitlab_mod.strip_draft_prefix("Normal MR"), "Normal MR")


if __name__ == "__main__":
    unittest.main()
