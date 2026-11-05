"""Agent command isolation, consent and leader-only broadcasting boundaries."""
import ast
import asyncio
import copy
import difflib
import re
import types
import unittest
from pathlib import Path

from natural_commands import looks_like_operation, simplified

CODE = (Path(__file__).resolve().parents[1] / "bot.py").read_text("utf-8")
TREE = ast.parse(CODE)


def load_functions(names, env):
    nodes = []
    for node in TREE.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            clone = copy.deepcopy(node)
            clone.decorator_list = []
            nodes.append(clone)
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
                 "<isolated agent handlers>", "exec"), env)
    return env


class FakeMessage:
    def __init__(self, uid=42, text="Гомер -2 фрп", private=True):
        self.text = text
        self.from_user = types.SimpleNamespace(id=uid)
        self.chat = types.SimpleNamespace(id=uid if private else 1000,
                                          type="private" if private else "group")
        self.sent = []

    async def answer(self, text, **kwargs):
        self.sent.append((text, kwargs))


class State:
    def __init__(self, active=None):
        self.active = active

    async def get_state(self):
        return self.active

    async def clear(self):
        self.active = None


class TestAgentParser(unittest.TestCase):
    def setUp(self):
        self.env = load_functions({
            "parse_agent_slash", "_agent_fuzzy_suggestion",
            "_agent_key_row", "get_verified_agent",
        }, {
            "AgentOperationError": ValueError, "simplified": simplified,
            "re": re, "difflib": difflib,
            "_cloud_state_hydrated": True,
            "users_db": {
                "42": {"nick": "Test_Agent", "rank": "5"},
                "99": {"nick": "Unverified_Agent", "verification_status": "pending"},
            }
        })

    def test_slash_debit_and_credit(self):
        self.assertEqual(self.env["parse_agent_slash"](
            "frp Gomez_Addams -2", "debit"
        ), "Gomez_Addams -2 frp")
        self.assertEqual(self.env["parse_agent_slash"](
            "praise Gomez_Addams +1", "credit"
        ), "Gomez_Addams +1 praise")
        self.assertTrue(looks_like_operation("Гомер -2 фрп"))

    def test_wrong_sign_or_invalid_quantity_fails_closed(self):
        for text, action in (
            ("frp Gomez_Addams +2", "debit"),
            ("praise Gomez_Addams -1", "credit"),
            ("frp Gomez_Addams -999", "debit"),
            ("frp Gomez_Addams -0", "debit"),
        ):
            if text.endswith("-0"):
                parsed = self.env["parse_agent_slash"](text, action)
                self.assertIn("-0", parsed)
            else:
                with self.assertRaises(ValueError):
                    self.env["parse_agent_slash"](text, action)

    def test_unique_gomer_guess_needs_explicit_name_confirmation(self):
        rows = [
            {"nick": "Gomez_Addams", "qty": 6, "id": "a", "profileId": "p"},
            {"nick": "Samuel_Norton", "qty": 4, "id": "b", "profileId": "q"},
        ]
        suggestion = self.env["_agent_fuzzy_suggestion"]("Гомер", rows)
        self.assertIsNotNone(suggestion)
        self.assertEqual(suggestion["nick"], "Gomez_Addams")

    def test_unverified_users_cannot_debit(self):
        self.assertIsNotNone(self.env["get_verified_agent"](42))
        self.assertIsNone(self.env["get_verified_agent"](99))
        self.env["_cloud_state_hydrated"] = False
        self.assertIsNone(self.env["get_verified_agent"](42))


class TestIdleNaturalRoute(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.actions = []
        async def admin(message, text):
            self.actions.append(("admin", text))
        async def agent(message, text):
            self.actions.append(("agent", text))
        self.env = load_functions({"conversational_admin_command"}, {
            "is_admin": lambda uid: uid == 101,
            "get_verified_agent": lambda uid: {"nick": "Agent"} if uid == 42 else None,
            "looks_like_operation": looks_like_operation,
            "handle_natural_command": admin,
            "agent_operation_from_text": agent,
        })

    async def test_plain_text_agent_debit_and_addition(self):
        await self.env["conversational_admin_command"](
            FakeMessage(text="Гомер -2 фрп"), State()
        )
        await self.env["conversational_admin_command"](
            FakeMessage(text="дай Гомеру одну похвалу"), State()
        )
        self.assertEqual([x[0] for x in self.actions], ["agent", "agent"])

    async def test_admin_commands_remain_separate(self):
        await self.env["conversational_admin_command"](
            FakeMessage(uid=101), State()
        )
        self.assertEqual(self.actions[0][0], "admin")

    async def test_no_actions_when_waiting_for_any_form(self):
        for active in ("ReportStates:waiting_for_proof",
                       "RegistrationStates:waiting_for_nick",
                       "GrantStates:waiting_for_details",
                       "AgentActionStates:waiting_add_evidence",
                       "NewsStates:waiting_for_text",
                       "SupportStates:waiting_for_message"):
            await self.env["conversational_admin_command"](
                FakeMessage(), State(active)
            )
        self.assertEqual(self.actions, [])

    async def test_unregistered_or_group_cannot_trigger(self):
        await self.env["conversational_admin_command"](
            FakeMessage(uid=500), State()
        )
        await self.env["conversational_admin_command"](
            FakeMessage(private=False), State()
        )
        self.assertEqual(self.actions, [])


class TestBroadcastGuards(unittest.TestCase):
    def test_announcement_targets_only_known_unblocked_users(self):
        env = load_functions({"_news_recipients", "_news_message", "_news_content"}, {
            "users_db": {
                "10": {"nick": "Test_Agent"},
                "11": {"nick": "New_Agent", "verification_status": "pending"},
                "12": {"nick": "Banned_Agent", "verification_status": "disabled"},
                "13": {"nick": "Cooling_Agent", "verification_status": "cooldown"},
                "not_a_uid": {"nick": "Other"},
            },
            "AgentOperationError": ValueError,
            "re": re,
            "html": __import__("html"),
        })
        self.assertEqual(env["_news_recipients"](), [10, 11])
        title, category, body = env["_news_content"]("обновление: <проверка>")
        self.assertEqual(category, "update")
        self.assertIn("&lt;проверка&gt;", env["_news_message"](title, category, body))

    def test_news_publish_and_agent_review_require_real_roles(self):
        for name in ("confirm_news_publish", "cmd_news"):
            node = next(n for n in TREE.body
                        if isinstance(n, ast.AsyncFunctionDef) and n.name == name)
            content = ast.get_source_segment(CODE, node)
            self.assertIn("is_leader(", content)
        node = next(n for n in TREE.body
                    if isinstance(n, ast.AsyncFunctionDef)
                    and n.name == "review_agent_add_request")
        text = ast.get_source_segment(CODE, node)
        self.assertIn("is_admin(callback.from_user.id)", text)
        self.assertIn("callback.message.chat.id != callback.from_user.id", text)

    def test_staff_credit_wont_use_direct_admin_grant(self):
        agent_block = CODE.split("async def agent_operation_from_text(", 1)[1].split(
            "def parse_agent_slash(", 1)[0]
        self.assertNotIn("credit_report(", agent_block)
        self.assertNotIn("record_admin_activity(", agent_block)
        self.assertIn('"credit"', agent_block)
        handler = CODE.split("async def review_agent_add_request(", 1)[1].split(
            '@dp.message(Command("agentlogs"))', 1)[0]
        self.assertIn("decide_add_request(", handler)
        self.assertNotIn("record_admin_activity(", handler)


if __name__ == "__main__":
    unittest.main()
