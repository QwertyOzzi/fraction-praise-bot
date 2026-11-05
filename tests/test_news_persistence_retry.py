"""News publication retains the preview on failure and retries the same ID.

Isolated handler tests use no Telegram credentials, network, or Firebase access.
"""
import ast
import asyncio
import copy
import html
import types
import unittest
from pathlib import Path

from agent_actions import AgentOperationError

CODE = (Path(__file__).resolve().parents[1] / "bot.py").read_text("utf-8")
TREE = ast.parse(CODE)


class Message:
    def __init__(self, uid=42):
        self.chat = types.SimpleNamespace(id=uid, type="private")
        self.edits = []

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, kwargs))


class Callback:
    def __init__(self, uid=42, nonce="a" * 16):
        self.from_user = types.SimpleNamespace(id=uid)
        self.message = Message(uid)
        self.data = "newspublish:" + nonce
        self.answers = []

    async def answer(self, text=None, **kwargs):
        self.answers.append((text, kwargs))


class State:
    def __init__(self, nonce="a" * 16):
        self.active = "news:confirm"
        self.data = {"news": {
            "title": "Новости FBI", "category": "news",
            "message": "Тест рассылки", "nonce": nonce,
            "recipients": [42, 43]
        }}
        self.cleared = 0

    async def get_state(self):
        return self.active

    async def get_data(self):
        return self.data

    async def clear(self):
        self.cleared += 1
        self.active = None


class Logger:
    def __init__(self):
        self.messages = []
    def warning(self, *args):
        self.messages.append(args)


def handler_env(env):
    node = next(n for n in TREE.body if isinstance(n, ast.AsyncFunctionDef)
                and n.name == "confirm_news_publish")
    node = copy.deepcopy(node)
    node.decorator_list = []
    env["types"] = types.SimpleNamespace(Message=Message, CallbackQuery=Callback)
    env["FSMContext"] = State
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])),
                 "<news-handler>", "exec"), env)
    return env


class TestNewsPersistenceRetry(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.created = []
        self.broadcast = []
        self.log = Logger()

        async def save(*args, **kwargs):
            self.created.append(kwargs)
            return {"id": kwargs["news_id"]}

        async def broadcast(*args):
            self.broadcast.append(args)

        self.env = handler_env({
            "is_leader": lambda uid: uid == 42,
            "has_permission": lambda uid, capability: uid == 42 and capability == "news.publish",
            "NewsStates": types.SimpleNamespace(
                confirm=types.SimpleNamespace(state="news:confirm")
            ),
            "_NEWS_SEND_LOCKS": {},
            "_NEWS_BROADCAST_TASKS": set(),
            "asyncio": asyncio, "html": html, "logger": self.log,
            "firebase_client": object(), "ClientSession": object(),
            "AgentOperationError": AgentOperationError,
            "create_announcement": save,
            "_broadcast_fbi_news": broadcast,
        })

    async def test_denied_firestore_keeps_preview_for_retry(self):
        async def denied(*args, **kwargs):
            raise AgentOperationError(
                "Firestore HTTP 403: доступ запрещён. Проверьте fbi_announcements"
            )
        self.env["create_announcement"] = denied
        state, callback = State(), Callback()
        await self.env["confirm_news_publish"](callback, state)
        self.assertEqual(state.cleared, 0)
        self.assertEqual(state.active, "news:confirm")
        self.assertEqual(self.created, [])
        self.assertEqual(self.broadcast, [])
        self.assertTrue(callback.answers[-1][1]["show_alert"])
        self.assertIn("HTTP 403", callback.answers[-1][0])

        async def success(*args, **kwargs):
            self.created.append(kwargs)
            return {"id": kwargs["news_id"]}
        self.env["create_announcement"] = success
        await self.env["confirm_news_publish"](callback, state)
        await asyncio.sleep(0)
        self.assertEqual(state.cleared, 1)
        self.assertEqual(self.created[0]["news_id"], "news-" + "a" * 16)
        self.assertEqual(len(self.broadcast), 1)

    async def test_repeated_callback_after_success_does_not_send_twice(self):
        state = State()
        await self.env["confirm_news_publish"](Callback(), state)
        await asyncio.sleep(0)
        await self.env["confirm_news_publish"](Callback(), state)
        self.assertEqual(len(self.created), 1)
        self.assertEqual(len(self.broadcast), 1)

    async def test_existing_journal_never_sends_same_news_twice(self):
        async def persisted(*args, **kwargs):
            self.created.append(kwargs)
            return {"id": kwargs["news_id"],
                    "status": "sending", "_already_persisted": True}
        self.env["create_announcement"] = persisted
        state, callback = State(), Callback()
        await self.env["confirm_news_publish"](callback, state)
        self.assertEqual(state.cleared, 1)
        self.assertEqual(self.broadcast, [])
        self.assertTrue(callback.answers[-1][1]["show_alert"])
        self.assertIn("/newslog", callback.message.edits[-1][0])

    async def test_different_nonce_does_not_publish_stale_draft(self):
        state = State()
        await self.env["confirm_news_publish"](
            Callback(nonce="b" * 16), state
        )
        self.assertEqual(state.cleared, 0)
        self.assertEqual(self.created, [])
        self.assertEqual(self.broadcast, [])

    async def test_nonleader_cannot_publish(self):
        state = State()
        await self.env["confirm_news_publish"](
            Callback(uid=99), state
        )
        self.assertEqual(self.created, [])
        self.assertEqual(state.cleared, 0)


if __name__ == "__main__":
    unittest.main()
