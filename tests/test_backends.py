"""AI 백엔드 레지스트리·어댑터·마스킹·미사용(none) 실행 정책 테스트.

외부 API 호출은 없다: 어댑터는 가짜 클라이언트·가짜 상수만 사용한다.
anthropic / python-dotenv 가 설치되지 않은 환경에서도 통과해야 한다.
"""

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import backends
import run_insight
import run_lifestyle
from backends import (
    AnthropicBackend,
    BackendCallError,
    BackendDisabled,
    BackendUnavailable,
    NullBackend,
    UnknownBackend,
    get_backend,
    mask_secrets,
    prepare_backend,
    resolve_backend_name,
)

FAKE_KEY = "sk-ant-" + "api03-ABCDEFGHIJKLMNOP1234567890"  # 합성 값 (실제 키 아님)
CONSTS = SimpleNamespace(
    MODEL_RESEARCH="m-research", MODEL_STRUCTURE="m-structure",
    RESEARCH_SYSTEM_PROMPT="rs", STRUCTURE_SYSTEM_PROMPT="ss", MAX_SEARCHES_PER_SECTION=7,
)
TOOL = {"name": "submit_x", "description": "d", "input_schema": {"type": "object", "properties": {}}}


class FakeClient:
    def __init__(self, content=None, error=None):
        self.calls = []
        self._content = content or []
        self._error = error
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self._error:
            raise self._error
        return SimpleNamespace(content=self._content)


def text(t):
    return SimpleNamespace(type="text", text=t)


def tool_use(name, data):
    return SimpleNamespace(type="tool_use", name=name, input=data)


def adapter(client, environ=None):
    return AnthropicBackend(client_factory=lambda: client, environ=environ or {"ANTHROPIC_API_KEY": "x"}, constants=CONSTS)


class NullBackendTests(unittest.TestCase):
    def test_disabled_calls_raise_and_not_available(self):
        b = NullBackend()
        self.assertEqual(b.name, "none")
        self.assertFalse(b.available()[0])
        with self.assertRaises(BackendDisabled):
            b.research("p")
        with self.assertRaises(BackendDisabled):
            b.structure("p", TOOL)


class RegistryTests(unittest.TestCase):
    def test_default_name_is_none(self):
        self.assertEqual(resolve_backend_name({}), "none")
        self.assertEqual(resolve_backend_name({"AI_BACKEND": "  "}), "none")
        self.assertEqual(resolve_backend_name({"AI_BACKEND": " Anthropic "}), "anthropic")

    def test_known_backends(self):
        self.assertIsInstance(get_backend("none"), NullBackend)
        self.assertIsInstance(get_backend("anthropic"), AnthropicBackend)
        self.assertEqual(sorted(backends.BACKENDS), ["anthropic", "none"])

    def test_unknown_backend_name_raises(self):
        with self.assertRaises(UnknownBackend) as cm:
            get_backend("bogus")
        self.assertIn("bogus", str(cm.exception))


class AnthropicAdapterTests(unittest.TestCase):
    def test_research_builds_request_and_joins_text(self):
        client = FakeClient([text("a"), SimpleNamespace(type="other"), text("b")])
        memo = adapter(client).research("user prompt")
        self.assertEqual(memo, "a\n\nb")
        call = client.calls[0]
        self.assertEqual(call["model"], "m-research")
        self.assertEqual(call["system"], "rs")
        self.assertEqual(call["tools"][0]["type"], "web_search_20250305")
        self.assertEqual(call["tools"][0]["max_uses"], 7)
        self.assertEqual(call["messages"], [{"role": "user", "content": "user prompt"}])

    def test_research_max_searches_override(self):
        client = FakeClient([text("a")])
        adapter(client).research("p", max_searches=3)
        self.assertEqual(client.calls[0]["tools"][0]["max_uses"], 3)

    def test_structure_returns_tool_input_and_forces_tool(self):
        client = FakeClient([text("ignored"), tool_use("submit_x", {"k": 1})])
        self.assertEqual(adapter(client).structure("u", TOOL), {"k": 1})
        call = client.calls[0]
        self.assertEqual(call["model"], "m-structure")
        self.assertEqual(call["tool_choice"], {"type": "tool", "name": "submit_x"})

    def test_structure_without_tool_call_raises(self):
        with self.assertRaises(BackendCallError):
            adapter(FakeClient([text("no tool")])).structure("u", TOOL)

    def test_sdk_error_is_wrapped_and_masked(self):
        err = RuntimeError(f"401 invalid x-api-key: {FAKE_KEY}")
        with self.assertRaises(BackendCallError) as cm:
            adapter(FakeClient(error=err), environ={"ANTHROPIC_API_KEY": FAKE_KEY}).research("p")
        self.assertNotIn(FAKE_KEY, str(cm.exception))
        self.assertIn("RuntimeError", str(cm.exception))

    def test_available_requires_key(self):
        ok, reason = AnthropicBackend(client_factory=object, environ={}).available()
        self.assertFalse(ok)
        self.assertIn("ANTHROPIC_API_KEY", reason)
        self.assertTrue(AnthropicBackend(client_factory=object, environ={"ANTHROPIC_API_KEY": "x"}).available()[0])

    def test_unavailable_without_key_raises_before_any_client(self):
        b = AnthropicBackend(client_factory=lambda: self.fail("client must not be built"), environ={}, constants=CONSTS)
        with self.assertRaises(BackendUnavailable):
            b.research("p")


class MaskTests(unittest.TestCase):
    def test_masks_tokens_keys_and_paths(self):
        samples = [
            FAKE_KEY,
            "ghp_" + "a" * 36,
            "github" + "_pat_" + "B" * 30,
            "Authorization: Bearer abcdef0123456789",
            "password=hunter2hunter2",
            'api_key = "zzzz-1234-5678"',
            "ey" + "JhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghij",
            "open C:" + "\\Users\\SomeName\\proj\\file.py failed",
            "cannot read /ho" + "me/someone/app/x",
        ]
        for s in samples:
            out = mask_secrets(s, {})
            for needle in (FAKE_KEY, "abcdef0123456789", "hunter2hunter2", "zzzz-1234-5678",
                           "SomeName", "someone", "a" * 36, "B" * 30):
                self.assertNotIn(needle, out, s)

    def test_masks_env_secret_value(self):
        out = mask_secrets("failed with key my-custom-secret-value", {"ANTHROPIC_API_KEY": "my-custom-secret-value"})
        self.assertNotIn("my-custom-secret-value", out)

    def test_plain_messages_unchanged(self):
        self.assertEqual(mask_secrets("RuntimeError: api down", {}), "RuntimeError: api down")


class PrepareBackendTests(unittest.TestCase):
    def _prepare(self, env):
        lines = []
        with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(backends, "load_env", lambda: False):
            for k in ("AI_BACKEND", "ANTHROPIC_API_KEY"):
                if k not in env:
                    os.environ.pop(k, None)
            result = prepare_backend(lines.append)
        return result, "\n".join(lines)

    def test_unset_is_skipped_exit_0(self):
        (backend, code), out = self._prepare({})
        self.assertIsNone(backend)
        self.assertEqual(code, 0)
        self.assertIn("STATUS=SKIPPED", out)
        self.assertIn("DATA_CHANGED=false", out)
        self.assertIn("EXIT_CODE=0", out)

    def test_none_is_skipped(self):
        (backend, code), out = self._prepare({"AI_BACKEND": "none"})
        self.assertEqual((backend, code), (None, 0))

    def test_unknown_name_is_error_exit_1(self):
        (backend, code), out = self._prepare({"AI_BACKEND": "bogus"})
        self.assertEqual((backend, code), (None, 1))
        self.assertIn("STATUS=ERROR", out)
        self.assertIn("EXIT_CODE=1", out)
        self.assertIn("DATA_CHANGED=false", out)

    def test_anthropic_without_key_is_error_exit_1(self):
        (backend, code), out = self._prepare({"AI_BACKEND": "anthropic"})
        self.assertEqual((backend, code), (None, 1))
        self.assertIn("STATUS=ERROR", out)

    def test_anthropic_ready_returns_backend(self):
        ready = lambda: AnthropicBackend(client_factory=object, environ={"ANTHROPIC_API_KEY": "x"})
        with mock.patch.dict(backends.BACKENDS, {"anthropic": ready}):
            (backend, code), _ = self._prepare({"AI_BACKEND": "anthropic"})
        self.assertIsInstance(backend, AnthropicBackend)
        self.assertIsNone(code)


class NoAiRunsLeaveDataUntouchedTests(unittest.TestCase):
    def _run(self, module, env):
        with tempfile.TemporaryDirectory() as t:
            base = Path(t)
            attrs = {"DATA_DIR": base / "data", "BASE_DIR": base}
            if module is run_lifestyle:
                attrs["INDEX_PATH"] = base / "data" / "insight_index.json"

            def boom(*a, **k):
                raise AssertionError("must not collect")

            collect_name = "collect_insight" if module is run_insight else "collect_lifestyle"
            buf = io.StringIO()
            with contextlib.ExitStack() as st:
                for k, v in attrs.items():
                    st.enter_context(mock.patch.object(module, k, v))
                st.enter_context(mock.patch.object(module, collect_name, boom))
                st.enter_context(mock.patch.object(backends, "load_env", lambda: False))
                st.enter_context(mock.patch.dict(os.environ, env))
                for k in ("AI_BACKEND", "ANTHROPIC_API_KEY"):
                    if k not in env:
                        os.environ.pop(k, None)
                with contextlib.redirect_stdout(buf):
                    code = module.main()
            leftovers = sorted(p.name for p in base.rglob("*"))
        return code, buf.getvalue(), leftovers

    def test_unset_backend_skips_without_touching_data(self):
        for module in (run_insight, run_lifestyle):
            with self.subTest(module=module.__name__):
                code, out, leftovers = self._run(module, {})
                self.assertEqual(code, 0)
                self.assertIn("STATUS=SKIPPED", out)
                self.assertEqual(leftovers, [])

    def test_anthropic_without_key_fails_without_touching_data(self):
        for module in (run_insight, run_lifestyle):
            with self.subTest(module=module.__name__):
                code, out, leftovers = self._run(module, {"AI_BACKEND": "anthropic"})
                self.assertEqual(code, 1)
                self.assertIn("EXIT_CODE=1", out)
                self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
