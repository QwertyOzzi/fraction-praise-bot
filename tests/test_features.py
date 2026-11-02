import ast
import asyncio
import html
import unittest
from pathlib import Path
from typing import Any, Dict, List

source = Path(__file__).resolve().parents[1].joinpath("bot.py").read_text(encoding="utf-8")
tree = ast.parse(source)
names = {"render_queue", "pending_report_keyboard", "pending_report_text",
         "send_pending_report_to_admin", "close_pending_report_messages"}
nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]

class Button:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

class Markup:
    def __init__(self, inline_keyboard):
        self.inline_keyboard = inline_keyboard

class FakeMessage:
    message_id = 101

class Bot:
    def __init__(self):
        self.calls = []
    async def send_message(self, *args, **kwargs):
        self.calls.append(("send", args, kwargs))
        return FakeMessage()
    async def pin_chat_message(self, **kwargs):
        self.calls.append(("pin", (), kwargs))
    async def unpin_chat_message(self, **kwargs):
        self.calls.append(("unpin", (), kwargs))
    async def edit_message_text(self, *args, **kwargs):
        self.calls.append(("edit", args, kwargs))

class Logger:
    def info(self, *args, **kwargs): pass

env = {"Dict": Dict, "List": List, "Any": Any, "html": html,
       "InlineKeyboardButton": Button, "InlineKeyboardMarkup": Markup,
       "bot": Bot(), "logger": Logger()}
exec(compile(ast.Module(body=nodes, type_ignores=[]), "<feature helpers>", "exec"), env)


class TestQueue(unittest.TestCase):
    def test_zero_quantity_disappears(self):
        text, keyboard = env["render_queue"]([{"nick": "Alice_River", "qty": 0}])
        self.assertIn("Очередь пуста", text)
        self.assertIsNone(keyboard)

    def test_positive_quantity_has_issue_button(self):
        text, keyboard = env["render_queue"]([
            {"nick": "Alice_River", "qty": 2},
            {"nick": "Bob_Smith", "qty": 0}
        ])
        self.assertIn("Alice_River", text)
        self.assertNotIn("Bob_Smith", text)
        self.assertEqual(len(keyboard.inline_keyboard), 1)
        self.assertEqual(keyboard.inline_keyboard[0][0].callback_data, "issue_1_Alice_River")

    def test_frp_approval_has_quantity_controls(self):
        req = {"rep_type": "fractionrp", "quantity": 4}
        keyboard = env["pending_report_keyboard"]("frp123", req)
        labels = [b.text for row in keyboard.inline_keyboard for b in row]
        self.assertIn("🎖️ 4", labels)
        self.assertIn("✅ Одобрить (+4)", labels)
        self.assertEqual(keyboard.inline_keyboard[0][0].callback_data, "qtyminus_frp123")
        self.assertEqual(keyboard.inline_keyboard[0][2].callback_data, "qtyplus_frp123")

    def test_praise_approval_has_quantity_controls(self):
        req = {"rep_type": "praise", "quantity": 3}
        keyboard = env["pending_report_keyboard"]("abcd", req)
        labels = [b.text for row in keyboard.inline_keyboard for b in row]
        self.assertIn("🌟 3", labels)
        self.assertIn("✅ Одобрить (+3)", labels)


class TestPinnedNotifications(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        env["bot"] = Bot()
        self.req = {
            "rep_type": "praise", "quantity": 2, "nick": "Alice_River",
            "reason": "отчёт", "rank": "8", "proof": "https://example.org",
            "admin_messages": []
        }

    async def test_pin_then_unpin_on_resolution(self):
        await env["send_pending_report_to_admin"](900, "abc", self.req)
        self.assertEqual(self.req["admin_messages"], [{"chat_id": 900, "message_id": 101}])
        await env["close_pending_report_messages"]("abc", self.req, "✅ ОДОБРЕНО")
        kinds = [x[0] for x in env["bot"].calls]
        self.assertEqual(kinds, ["send", "pin", "edit", "unpin"])


if __name__ == "__main__":
    unittest.main()
