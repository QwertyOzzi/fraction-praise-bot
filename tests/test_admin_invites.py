import ast
import datetime
import html
import re
import types
import unittest
import uuid
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1].joinpath("bot.py").read_text("utf-8")
TREE = ast.parse(SOURCE)


class Button:
    def __init__(self, **kwargs): self.__dict__.update(kwargs)


class Markup:
    def __init__(self, inline_keyboard): self.inline_keyboard = inline_keyboard


class State:
    async def clear(self): pass


class Message:
    def __init__(self, text, user_id=6060562962, private=True):
        self.text = text
        self.from_user = types.SimpleNamespace(id=user_id, full_name="Owner", username="owner")
        self.chat = types.SimpleNamespace(type="private" if private else "group")
        self.answers = []
    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))


class Callback:
    def __init__(self, data, user_id=6060562962):
        self.data = data
        self.from_user = types.SimpleNamespace(id=user_id)
        self.answers = []
    async def answer(self, text, **kwargs):
        self.answers.append(text)


def load_functions(names, other=None):
    nodes = []
    for node in TREE.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            node.decorator_list = []
            nodes.append(node)
    env = {
        "re": re, "html": html, "uuid": uuid, "datetime": datetime.datetime,
        "timezone": datetime.timezone, "types": types,
        "InlineKeyboardButton": Button, "InlineKeyboardMarkup": Markup,
        "is_owner": lambda x: x == 6060562962,
        "is_admin": lambda x: x == 6060562962,
        "OWNER_TELEGRAM_IDS": {6060562962},
        "users_db": {"101": {"nick": "Test_User", "rank": "9", "telegram_username": "Known_User"}},
        "pending_admin_requests": {}, "admins_db": [],
    }
    if other:
        env.update(other)
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
                 "<admin commands>", "exec"), env)
    return env


class TestAdminInvites(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = load_functions({"cmd_admin_auth", "verify_admin_request"})
        self.env["DEPUTY_KEY"] = "deputy-key-for-tests"

    async def test_owner_invites_registered_user(self):
        m = Message("/admin @Known_User")
        await self.env["cmd_admin_auth"](m, State())
        self.assertEqual(len(self.env["pending_admin_requests"]), 1)
        kb = m.answers[-1][1]["reply_markup"]
        data = kb.inline_keyboard[0][0].callback_data
        self.assertEqual(self.env["verify_admin_request"](data, "adm_grant_"), 101)

    async def test_non_owner_cannot_direct_invite(self):
        m = Message("/admin @Known_User", user_id=111)
        await self.env["cmd_admin_auth"](m, State())
        self.assertEqual(self.env["pending_admin_requests"], {})
        self.assertIn("только владелец", m.answers[0][0])

    async def test_unregistered_user_cannot_be_granted(self):
        m = Message("/admin @Unseen_User")
        await self.env["cmd_admin_auth"](m, State())
        self.assertEqual(self.env["pending_admin_requests"], {})

    async def test_group_invite_rejected(self):
        m = Message("/admin @Known_User", private=False)
        await self.env["cmd_admin_auth"](m, State())
        self.assertEqual(self.env["pending_admin_requests"], {})

    async def test_old_callback_and_bad_nonce_rejected(self):
        self.assertIsNone(self.env["verify_admin_request"]("adm_grant_101", "adm_grant_"))
        self.assertIsNone(self.env["verify_admin_request"]("adm_grant_101_deadbeefdeadbeef", "adm_grant_"))

    async def test_expired_request_rejected(self):
        self.env["pending_admin_requests"][101] = {
            "expires": 1, "nonce": "abcdef1234567890", "source": "owner"
        }
        self.assertIsNone(self.env["verify_admin_request"](
            "adm_grant_101_abcdef1234567890", "adm_grant_"))


class TestAdminAuthStatic(unittest.TestCase):
    def test_grant_callback_owner_only(self):
        section = SOURCE.split("async def callback_admin_grant(", 1)[1].split(
            "async def callback_admin_deny(", 1)[0]
        self.assertIn("is_owner(callback.from_user.id)", section)
        self.assertIn("verify_admin_request(", section)
        self.assertIn("pending_admin_requests.pop(", section)


if __name__ == "__main__":
    unittest.main()
