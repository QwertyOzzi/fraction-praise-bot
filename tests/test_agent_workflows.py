"""Agent callbacks and news permission regressions without live credentials."""
import ast
import asyncio
import copy
import html
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path

BOT = (Path(__file__).resolve().parents[1] / "bot.py").read_text("utf-8")
TREE = ast.parse(BOT)


class Message:
    def __init__(self, *, chat_id=42):
        self.chat = types.SimpleNamespace(id=chat_id, type="private")
        self.edits = []
        self.replies = []

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, kwargs))

    async def answer(self, text, **kwargs):
        self.replies.append((text, kwargs))


class Callback:
    def __init__(self, action, *, user_id=42):
        self.data = action
        self.from_user = types.SimpleNamespace(id=user_id)
        self.message = Message(chat_id=user_id)
        self.answers = []

    async def answer(self, text=None, **kwargs):
        self.answers.append((text, kwargs))


class State:
    def __init__(self):
        self.value = None
        self.data = {}

    async def get_state(self):
        return self.value

    async def set_state(self, value):
        self.value = value

    async def update_data(self, **kwargs):
        self.data.update(kwargs)


def load_functions(names, env):
    funcs = []
    for node in TREE.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name in names:
            function = copy.deepcopy(node)
            function.decorator_list = []
            funcs.append(function)
    env.setdefault("types", types.SimpleNamespace(
        CallbackQuery=Callback, Message=Message
    ))
    env.setdefault("FSMContext", State)
    exec(compile(ast.fix_missing_locations(ast.Module(body=funcs, type_ignores=[])),
                 "isolated agent handlers", "exec"), env)
    return env


class TestAgentCallbacks(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.calls = []
        async def apply(*args, **kwargs):
            self.calls.append((args, kwargs))
            return "applied"
        async def audit(*args, **kwargs):
            return None
        self.token = "a" * 20
        self.task = {
            "owner": 42, "chat": 42,
            "created": datetime.now(timezone.utc).timestamp(),
            "status": "preview", "raw": "Гомер -2 фрп",
            "alias": "Гомер",
            "operation_id": "ai-agent-test",
            "plan": {"action": "debit", "sheet": "fractionrp", "amount": 2,
                     "names": ["Гомер"]},
            "target": {"nick": "Gomez_Addams", "qty": 5,
                       "id": "row1", "profileId": "a", "rank": "8"}
        }
        self.env = load_functions({"agent_points_decision"}, {
            "AGENT_TASKS": {self.token: self.task},
            "_AGENT_TASK_LOCKS": {}, "_AGENT_TASK_TTL": 600,
            "users_db": {"42": {"nick": "Test_Agent"}},
            "active_profile": lambda profile: True,
            "AgentPointStates": types.SimpleNamespace(
                waiting_for_reason="agent-reason"
            ),
            "firebase_client": object(), "ClientSession": object(),
            "apply_batch": apply, "agent_audit_event": audit,
            "asyncio": asyncio, "datetime": datetime, "timezone": timezone,
            "html": html, "BalanceConflict": ValueError,
        })

    async def test_agent_debit_never_calls_admin_action_or_approval(self):
        call = Callback("agentconfirm:" + self.token)
        await self.env["agent_points_decision"](call, State())
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0][3], "debit")
        self.assertEqual(self.calls[0][1]["agent_audit"]["actor_id"], 42)
        self.assertEqual(self.task["status"], "done")

    async def test_double_click_only_one_atomic_write(self):
        calls = [Callback("agentconfirm:" + self.token),
                 Callback("agentconfirm:" + self.token)]
        await asyncio.gather(*(self.env["agent_points_decision"](c, State())
                               for c in calls))
        self.assertEqual(len(self.calls), 1)

    async def test_plus_only_asks_for_reason_and_does_not_credit(self):
        self.task["plan"]["action"] = "credit"
        state = State()
        await self.env["agent_points_decision"](
            Callback("agentconfirm:" + self.token), state
        )
        self.assertEqual(self.calls, [])
        self.assertEqual(self.task["status"], "reason")
        self.assertEqual(state.data["agent_task"], self.token)

    async def test_other_user_cannot_replay_agent_button(self):
        other = Callback("agentconfirm:" + self.token, user_id=99)
        await self.env["agent_points_decision"](other, State())
        self.assertFalse(self.calls)
        self.assertTrue(other.answers[-1][1]["show_alert"])


class TestAgentReview(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.calls = []
        self.saved = []
        async def apply(*args, **kwargs):
            self.calls.append((args, kwargs))
            return "applied"
        async def sync():
            self.saved.append("ok")
            return True
        async def note(*args, **kwargs):
            return None
        async def audit(*args, **kwargs):
            return None
        class Firebase:
            async def get_sheet(self, sheet):
                return [{"nick": "Gomez_Addams", "qty": 5, "id": "id1",
                         "profileId": "p1", "rank": "7"}]
        self.rid = "b" * 16
        self.req = {
            "req_id": self.rid, "status": "pending",
            "user_id": "42", "actor_nick": "Test_Agent",
            "target_nick": "Gomez_Addams", "target_id": "id1",
            "target_profile_id": "p1", "sheet": "fractionrp",
            "amount": 2, "reason": "Полезная работа",
            "operation_id": "ai-agent-confirmed-req"
        }
        self.env = load_functions({"agent_request_decision"}, {
            "is_admin": lambda uid: uid == 7,
            "users_db": {"42": {"nick": "Test_Agent"}},
            "active_profile": lambda p: True,
            "agent_requests_db": {self.rid: self.req},
            "_AGENT_TASK_LOCKS": {}, "asyncio": asyncio,
            "sync_state_to_cloud": sync, "firebase_client": Firebase(),
            "ClientSession": object(), "apply_batch": apply,
            "BalanceConflict": ValueError, "notify_agent_request_result": note,
            "agent_audit_event": audit, "logger": types.SimpleNamespace(
                warning=lambda *args: None
            )
        })

    async def test_only_real_leadership_can_approve_agent_credit(self):
        await self.env["agent_request_decision"](
            Callback("agentapprove:" + self.rid, user_id=99)
        )
        self.assertFalse(self.calls)
        self.assertEqual(self.req["status"], "pending")

    async def test_credit_only_after_approval_and_no_duplicate(self):
        tasks = [Callback("agentapprove:" + self.rid, user_id=7),
                 Callback("agentapprove:" + self.rid, user_id=7)]
        await asyncio.gather(*(self.env["agent_request_decision"](c)
                               for c in tasks))
        self.assertEqual(self.req["status"], "approved")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0][3], "credit")
        self.assertEqual(self.calls[0][1]["agent_audit"]["actor_id"], 42)
        self.assertEqual(self.calls[0][1]["agent_audit"]["reviewer_id"], 7)

    async def test_reject_never_changes_balance(self):
        await self.env["agent_request_decision"](
            Callback("agentdeny:" + self.rid, user_id=7)
        )
        self.assertEqual(self.req["status"], "rejected")
        self.assertEqual(self.calls, [])
        self.assertTrue(self.saved)


class FakeBot:
    def __init__(self):
        self.copies = []

    async def copy_message(self, **kwargs):
        self.copies.append(kwargs)


class TestNewsBroadcast(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = FakeBot()
        self.token = "d" * 20
        self.draft = {
            "owner": 7, "chat": 7, "source_message": 501,
            "created": datetime.now(timezone.utc).timestamp(),
            "status": "preview", "category": "update",
            "recipients": [7, 42, 59]
        }
        self.logs = []
        async def write_log(*args, **kwargs):
            self.logs.append(kwargs)
            return True
        async def fast_sleep(seconds):
            return None
        self.env = load_functions({"news_confirm"}, {
            "is_leader": lambda uid: uid == 7,
            "NEWS_DRAFTS": {self.token: self.draft},
            "bot": self.bot, "asyncio": types.SimpleNamespace(
                sleep=fast_sleep
            ), "datetime": datetime, "timezone": timezone,
            "save_broadcast_log": write_log,
            "firebase_client": object(), "ClientSession": object(),
            "logger": types.SimpleNamespace(
                info=lambda *args: None, warning=lambda *args: None
            )
        })

    async def test_nonleader_cannot_broadcast(self):
        await self.env["news_confirm"](
            Callback("newssend:" + self.token, user_id=99)
        )
        self.assertEqual(self.bot.copies, [])

    async def test_sends_once_to_expected_recipients(self):
        calls = [Callback("newssend:" + self.token, user_id=7),
                 Callback("newssend:" + self.token, user_id=7)]
        for callback in calls:
            await self.env["news_confirm"](callback)
        self.assertEqual(self.draft["status"], "done")
        self.assertEqual(
            [call["chat_id"] for call in self.bot.copies], [7, 42, 59]
        )
        self.assertEqual(len(self.logs), 1)
        self.assertEqual(self.logs[0]["delivered"], 3)
        self.assertEqual(self.logs[0]["failed"], 0)

    async def test_news_cancel_sends_nothing(self):
        await self.env["news_confirm"](
            Callback("newscancel:" + self.token, user_id=7)
        )
        self.assertEqual(self.draft["status"], "cancelled")
        self.assertEqual(self.bot.copies, [])


class TestStaticIsolation(unittest.TestCase):
    def test_admin_grants_are_not_called_by_agent_code(self):
        for fname in ("agent_points_decision", "agent_request_decision",
                      "receive_agent_point_reason"):
            fn = next(node for node in TREE.body
                      if isinstance(node, ast.AsyncFunctionDef)
                      and node.name == fname)
            raw = ast.get_source_segment(BOT, fn)
            self.assertNotIn("record_admin_activity", raw)
            self.assertNotIn("perform_manual_grant", raw)
        self.assertIn("agent_point_requests", BOT)

    def test_news_and_agent_proof_states_keep_implicit_ai_idle(self):
        fn = next(node for node in TREE.body
                  if isinstance(node, ast.AsyncFunctionDef)
                  and node.name == "conversational_admin_command")
        self.assertIn("await state.get_state()", ast.get_source_segment(BOT, fn))


if __name__ == "__main__":
    unittest.main()
