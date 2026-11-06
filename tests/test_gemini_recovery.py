"""Offline regression tests for Gemini overload recovery (no API key or network)."""
import ast
import asyncio
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from natural_commands import MODEL, ParseError

BOT = (Path(__file__).resolve().parents[1] / "bot.py").read_text("utf-8")
NODE = next(
    node for node in ast.parse(BOT).body
    if isinstance(node, ast.AsyncFunctionDef) and node.name == "gemini_json_request"
)


class FakeResponse:
    def __init__(self, status):
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self):
        return {"candidates": [{"content": {"parts": [
            {"text": '{"action":"credit"}'}
        ]}}]}


class FakeSession:
    def __init__(self, responses, calls):
        self.responses = responses
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def post(self, url, headers, json):
        self.calls.append(url)
        return FakeResponse(self.responses.pop(0))


class TestGeminiRecovery(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.responses = []
        self.calls = []
        env = {
            "asyncio": asyncio,
            "ClientTimeout": lambda **kwargs: object(),
            "ClientSession": lambda timeout: FakeSession(self.responses, self.calls),
            "ClientError": type("FakeClientError", (Exception,), {}),
            "ParseError": ParseError,
            "MODEL": MODEL,
            "_AI_SEMAPHORE": asyncio.Semaphore(3),
            "logger": types.SimpleNamespace(warning=lambda *args, **kwargs: None),
        }
        exec(compile(ast.fix_missing_locations(ast.Module(
            body=[NODE], type_ignores=[]
        )), "<gemini reliability>", "exec"), env)
        self.call = env["gemini_json_request"]
        self.url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{MODEL}:generateContent"
        )

    async def request(self):
        async def no_delay(_):
            return None
        with patch.object(asyncio, "sleep", no_delay):
            return await self.call(self.url, {"x-goog-api-key": "mock"}, {})

    async def test_retry_and_fallback_on_503(self):
        self.responses[:] = [503, 503, 200]
        result = await self.request()
        self.assertIn("candidates", result)
        self.assertEqual(len(self.calls), 3)
        self.assertIn("gemini-3.8-flash", self.calls[0])
        self.assertIn("gemini-3.7-flash", self.calls[-1])

    async def test_auth_failure_never_retries(self):
        self.responses[:] = [403]
        with self.assertRaisesRegex(ParseError, "ключ"):
            await self.request()
        self.assertEqual(len(self.calls), 1)

    async def test_quota_failure_never_retries(self):
        self.responses[:] = [429]
        with self.assertRaisesRegex(ParseError, "квот"):
            await self.request()
        self.assertEqual(len(self.calls), 1)

    async def test_permanent_overload_fails_readably(self):
        self.responses[:] = [503, 500, 503]
        with self.assertRaisesRegex(ParseError, "Повторные попытки"):
            await self.request()
        self.assertEqual(len(self.calls), 3)


if __name__ == "__main__":
    unittest.main()
