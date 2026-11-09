"""Regression for AI debit reason prompt: no external API or Firebase required."""
import ast
import asyncio
from datetime import datetime, timezone
import html
from pathlib import Path
import types
import unittest

from natural_commands import looks_like_operation, simple_parse, split_operation_reason


SOURCE = (Path(__file__).resolve().parents[1] / "bot.py").read_text("utf-8")
TREE = ast.parse(SOURCE)


class Message:
    def __init__(self, text=""):
        self.from_user = types.SimpleNamespace(id=101)
        self.chat = types.SimpleNamespace(id=101, type="private")
        self.text = text
        self.replies = []

    async def answer(self, message, **kwargs):
        self.replies.append(message)


class State:
    def __init__(self, data=None):
        self.data = dict(data or {})
        self.active = None
        self.cleared = 0

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **data):
        self.data.update(data)

    async def set_state(self, state):
        self.active = state

    async def clear(self):
        self.data.clear()
        self.active = None
        self.cleared += 1


def isolate(function, env):
    node = next(x for x in TREE.body if isinstance(x, ast.AsyncFunctionDef)
                and x.name == function)
    node.decorator_list = []
    env.update({
        "types": types.SimpleNamespace(Message=Message),
        "FSMContext": State,
    })
    exec(compile(ast.fix_missing_locations(ast.Module(
        body=[node], type_ignores=[])), "<isolated AI reason>", "exec"), env)
    return env[function]


class TestReasonPrompt(unittest.IsolatedAsyncioTestCase):
    async def test_missing_reason_asks_followup_and_saves_no_action(self):
        async def local_interpret(*args):
            return {"action": "debit", "sheet": "fractionrp",
                    "amount": 1, "names": ["гомез"]}, "local"

        env = {
            "is_admin": lambda uid: uid == 101,
            "split_operation_reason": split_operation_reason,
            "datetime": datetime, "timezone": timezone, "_AI_LIMIT": {},
            "interpret_natural": local_interpret, "GEMINI_API_KEY": "",
            "gemini_json_request": None, "has_permission": lambda *a: True,
            "debit_limit": lambda uid: 5, "html": html,
            "AiStates": types.SimpleNamespace(waiting_for_debit_reason="debit_reason"),
        }
        handle = isolate("handle_natural_command", env)
        message, state = Message("гомез -1 фрп"), State()
        await handle(message, message.text, state)
        self.assertEqual(state.active, "debit_reason")
        self.assertEqual(state.data["ai_debit_command"], message.text)
        self.assertIn("Укажите причину", message.replies[-1])
        self.assertIn("ничего не списано", message.replies[-1])
        self.assertEqual(env["_AI_LIMIT"][101].__len__(), 1)

    async def test_one_line_screenshot_reason_splits_correctly(self):
        for raw in ("гомез -1 фрп выдали в игре",
                    "гомез -1 фрп | выдали в игре",
                    "гомез фрп -1 | выдали в игре",
                    "Гомез −1 фрп | выдали в игре"):
            command, reason, has_reason = split_operation_reason(raw)
            self.assertTrue(has_reason, raw)
            self.assertEqual(reason, "выдали в игре")
            plan = simple_parse(command)
            self.assertIsNotNone(plan, raw)
            self.assertEqual(plan["action"], "debit")
            self.assertEqual(plan["amount"], 1)


class TestReasonReply(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.calls = []
        async def handle(*args, **kwargs):
            self.calls.append((args, kwargs))
        env = {
            "is_admin": lambda uid: uid == 101,
            "looks_like_operation": looks_like_operation,
            "handle_natural_command": handle,
        }
        self.receive = isolate("receive_ai_debit_reason", env)

    async def test_plain_reason_reuses_pending_command(self):
        message = Message("выдали в игре")
        state = State({"ai_debit_command": "гомез -1 фрп"})
        await self.receive(message, state)
        self.assertEqual(self.calls[0][0][1], "гомез -1 фрп | выдали в игре")
        self.assertTrue(self.calls[0][1]["reason_reply"])
        self.assertEqual(state.cleared, 1)

    async def test_complete_replacement_command(self):
        message = Message("гомез -1 фрп | выдали в игре")
        state = State({"ai_debit_command": "норто -3 фрп"})
        await self.receive(message, state)
        self.assertEqual(self.calls[0][0][1], message.text)
        self.assertFalse(self.calls[0][1])
        self.assertEqual(state.cleared, 1)

    async def test_invalid_reason_is_not_used(self):
        for text in ("нет", "/start", "причина | небезопасно"):
            with self.subTest(text=text):
                message = Message(text)
                state = State({"ai_debit_command": "гомез -1 фрп"})
                await self.receive(message, state)
                self.assertEqual(state.cleared, 0)
                self.assertEqual(self.calls, [])
                self.assertTrue(message.replies)

    async def test_missing_state_fails_closed(self):
        message, state = Message("выдали в игре"), State()
        await self.receive(message, state)
        self.assertEqual(self.calls, [])
        self.assertIn("устарела", message.replies[-1])


if __name__ == "__main__":
    unittest.main()
