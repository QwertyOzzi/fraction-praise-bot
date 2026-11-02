import ast
import asyncio
import hashlib
import html
import re
import types
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

SOURCE = Path(__file__).resolve().parents[1].joinpath("bot.py").read_text("utf-8")
TREE = ast.parse(SOURCE)


class Button:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class Markup:
    def __init__(self, inline_keyboard):
        self.inline_keyboard = inline_keyboard


class Logger:
    def error(self, *args): pass
    def warning(self, *args): pass


class Message:
    def __init__(self, uid=6060562962):
        self.from_user = types.SimpleNamespace(id=uid)
        self.chat = types.SimpleNamespace(id=uid)
        self.answers = []
        self.updates = []

    async def answer(self, text, **kwargs):
        self.answers.append(text)

    async def edit_text(self, text, **kwargs):
        self.updates.append(text)


class Callback:
    def __init__(self, data, uid=6060562962):
        self.from_user = types.SimpleNamespace(id=uid)
        self.message = Message(uid)
        self.data = data
        self.answers = []

    async def answer(self, *args, **kwargs):
        self.answers.append(args[0] if args else "")


class FakeFirebase:
    def __init__(self):
        self.credits = []
        self.debits = []

    async def credit_report(self, *args, **kwargs):
        self.credits.append((args, kwargs))
        await asyncio.sleep(0)
        return True

    async def debit_one(self, sheet, nick):
        self.debits.append((sheet, nick))
        return 0


def load_functions(names, extra=None):
    nodes = []
    for node in TREE.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            node.decorator_list = []
            nodes.append(node)
    env = {
        "re": re, "html": html, "hashlib": hashlib,
        "uuid": uuid, "asyncio": asyncio, "datetime": datetime, "timezone": timezone,
        "Any": Any, "Dict": Dict, "List": List,
        "types": types.SimpleNamespace(CallbackQuery=Callback, Message=Message),
        "InlineKeyboardButton": Button, "InlineKeyboardMarkup": Markup,
        "logger": Logger(), "is_admin": lambda uid: uid == 6060562962,
    }
    if extra:
        env.update(extra)
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
                 "<agent panel>", "exec"), env)
    return env


class TestBulkGrantParser(unittest.TestCase):
    def setUp(self):
        self.parse = load_functions({"parse_grant_args"})["parse_grant_args"]

    def test_ten_recipients(self):
        raw = "praise " + ",".join("nick" + str(i) for i in range(10)) + " 3"
        sheet, names, quantity = self.parse(raw)
        self.assertEqual(sheet, "praise")
        self.assertEqual(len(names), 10)
        self.assertEqual(quantity, 3)

    def test_spaces_after_commas(self):
        self.assertEqual(self.parse("frp Nick_One, Nick_Two 2"),
                         ("fractionrp", ["Nick_One", "Nick_Two"], 2))

    def test_reject_more_than_ten(self):
        with self.assertRaisesRegex(ValueError, "10"):
            self.parse("frp " + ",".join("n" + str(i) for i in range(11)) + " 1")

    def test_reject_duplicate_casefold(self):
        with self.assertRaisesRegex(ValueError, "повторять"):
            self.parse("frp Nick_One,nick_one 2")

    def test_reject_empty_and_invalid_recipient(self):
        for sample in ("frp nick1,,nick2 2", "frp nick1,@bob 2", "frp nick1, 2"):
            with self.subTest(sample=sample), self.assertRaises(ValueError):
                self.parse(sample)

    def test_reject_invalid_quantity(self):
        for amount in (0, 21):
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                self.parse(f"frp user1,user2 {amount}")


class TestAgentRendering(unittest.TestCase):
    def setUp(self):
        env = load_functions({"agent_hash", "agent_list_markup", "agent_detail_markup"})
        self.ag_hash = env["agent_hash"]
        self.list_markup = env["agent_list_markup"]
        self.detail_markup = env["agent_detail_markup"]

    def test_pagination(self):
        agents = [{"nick": "nick" + str(i), "rank": "8", "frp": 0, "praise": 0}
                  for i in range(18)]
        text, buttons = self.list_markup(agents, 1)
        self.assertIn("2/3", text)
        self.assertEqual(sum(any(b.callback_data.startswith("agview:") for b in row)
                             for row in buttons.inline_keyboard), 8)

    def test_detail_buttons_and_stable_lookup(self):
        agent = {"nick": "Test_Gomez", "rank": "7", "frp": 2,
                 "praise": 1, "frp_status": "На выдаче",
                 "praise_status": "На выдаче", "uid": "123"}
        text, keyboard = self.detail_markup(agent, 0)
        self.assertIn("Test_Gomez", text)
        data = [button.callback_data for row in keyboard.inline_keyboard for button in row]
        self.assertIn(f"agask:g:f:{self.ag_hash('Test_Gomez')}:5:0", data)
        self.assertIn(f"agask:d:p:{self.ag_hash('Test_Gomez')}:1:0", data)
        self.assertIn(f"agranks:{self.ag_hash('Test_Gomez')}:0", data)
        self.assertTrue(all(len(value.encode("utf-8")) <= 64 for value in data))


class TestPanelTransactions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = FakeFirebase()
        self.actions = {}
        self.rendered = []
        async def show_detail(message, key, page):
            self.rendered.append((key, page))
        self.env = load_functions({"agent_do_action"}, {
            "agent_action_tokens": self.actions,
            "firebase_client": self.db,
            "agent_panel_detail": show_detail,
        })
        self.token = "e" * 16
        self.actions[self.token] = {
            "uid": 6060562962, "chat_id": 6060562962,
            "nick": "Test_Gomez", "rank": "7",
            "kind": "g", "which": "p", "qty": 5,
            "page": 0, "key": "abc123",
            "created": datetime.now(timezone.utc).timestamp(), "status": "new"
        }

    async def test_confirm_credit_only_once_even_simultaneously(self):
        a = Callback("agdo:" + self.token)
        b = Callback("agdo:" + self.token)
        await asyncio.gather(self.env["agent_do_action"](a),
                             self.env["agent_do_action"](b))
        self.assertEqual(len(self.db.credits), 1)
        self.assertEqual(self.db.credits[0][1]["quantity"], 5)
        self.assertEqual(self.db.credits[0][0][-1], "panel-" + self.token)

    async def test_non_admin_cannot_confirm(self):
        attempt = Callback("agdo:" + self.token, uid=12345)
        await self.env["agent_do_action"](attempt)
        self.assertEqual(self.db.credits, [])

    async def test_unknown_token_does_not_credit(self):
        attempt = Callback("agdo:unknown")
        await self.env["agent_do_action"](attempt)
        self.assertEqual(self.db.credits, [])


if __name__ == "__main__":
    unittest.main()
