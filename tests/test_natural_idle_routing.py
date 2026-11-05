"""Natural commands work automatically only when the private admin chat is idle."""
import ast
import copy
import types
import unittest
from pathlib import Path

BOT = (Path(__file__).resolve().parents[1] / "bot.py").read_text("utf-8")
TREE = ast.parse(BOT)


class Message:
    def __init__(self, text="норто, гоммез фрп +1", private=True):
        self.from_user = types.SimpleNamespace(id=101)
        self.chat = types.SimpleNamespace(
            id=101 if private else 501, type="private" if private else "group"
        )
        self.text = text
        self.responses = []

    async def answer(self, message, **kwargs):
        self.responses.append(message)


class State:
    def __init__(self, active=None):
        self.active = active
        self.clears = 0

    async def get_state(self):
        return self.active

    async def clear(self):
        self.clears += 1
        self.active = None

    async def set_state(self, value):
        self.active = value


class Callback:
    def __init__(self):
        self.data = "main:ai"
        self.from_user = types.SimpleNamespace(id=101)
        self.message = Message()
        self.replies = []

    async def answer(self, text=None, **kwargs):
        self.replies.append((text, kwargs))


def isolate(names, env):
    # Annotated handlers reference aiogram types at function-definition time,
    # even though these tests deliberately run without aiogram installed.
    env["types"] = types.SimpleNamespace(Message=Message, CallbackQuery=Callback)
    env["FSMContext"] = State
    funcs = []
    for node in TREE.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name in names:
            fn = copy.deepcopy(node)
            fn.decorator_list = []
            funcs.append(fn)
    exec(compile(ast.fix_missing_locations(ast.Module(body=funcs, type_ignores=[])),
                 "<isolated routing>", "exec"), env)
    return env


class TestNaturalOnlyWhenIdle(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.commands = []
        async def handle(message, text):
            self.commands.append(text)
        self.env = isolate({"conversational_admin_command"}, {
            "is_admin": lambda uid: uid == 101,
            "looks_like_operation": lambda text: "фрп" in text,
            "handle_natural_command": handle,
            "get_verified_agent": lambda uid: None,
        })

    async def test_only_idle_admin_message_triggers_ai(self):
        await self.env["conversational_admin_command"](
            Message(), State()
        )
        self.assertEqual(len(self.commands), 1)

    async def test_no_ai_when_bot_expects_other_inputs(self):
        for active in (
            "RegistrationStates:waiting_for_nick",
            "RegistrationStates:waiting_for_rank",
            "ReportStates:waiting_for_quantity",
            "ReportStates:waiting_for_reason",
            "ReportStates:waiting_for_proof",
            "GrantStates:waiting_for_details",
            "SupportStates:waiting_for_message",
            "AgentPanelStates:waiting_for_search",
        ):
            await self.env["conversational_admin_command"](
                Message("норто, гоммез фрп +1"), State(active)
            )
        self.assertEqual(self.commands, [])

    async def test_unprivileged_and_group_messages_do_nothing(self):
        msg = Message()
        msg.from_user.id = 222
        await self.env["conversational_admin_command"](msg, State())
        await self.env["conversational_admin_command"](
            Message(private=False), State()
        )
        self.assertEqual(self.commands, [])


class TestExplicitAiDoesNotCancelForms(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.commands = []
        async def ai(message, state):
            self.commands.append("ai")
        self.env = isolate({"cmd_quick_navigation"}, {
            "cmd_ai": ai,
            "cmd_my_status": ai,
            "start_report": ai,
            "cmd_change_nick": ai,
            "cmd_change_rank": ai,
            "cmd_support": ai,
        })

    async def test_ai_does_not_clear_any_existing_wizard(self):
        for active in (
            "ReportStates:waiting_for_proof",
            "RegistrationStates:waiting_for_nick",
            "ReportStates:waiting_for_quantity",
            "GrantStates:waiting_for_details",
        ):
            state = State(active)
            message = Message("/ai")
            await self.env["cmd_quick_navigation"](message, state)
            self.assertEqual(state.active, active)
            self.assertEqual(state.clears, 0)
            self.assertIn("/cancel", message.responses[-1])
        self.assertEqual(self.commands, [])

    async def test_explicit_ai_still_works_when_idle(self):
        state = State()
        await self.env["cmd_quick_navigation"](Message("/ai"), state)
        self.assertEqual(self.commands, ["ai"])
        self.assertEqual(state.clears, 1)


class TestInlineMenuAiDoesNotCancelWizard(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = isolate({"main_menu_callback"}, {
            "MAIN_MENU_ADMIN_ACTIONS": {"ai", "admin"},
            "types": types.SimpleNamespace(Message=Message),
            "is_admin": lambda uid: uid == 101,
        })
    async def test_inline_ai_blocked_when_busy(self):
        callback, state = Callback(), State("ReportStates:waiting_for_proof")
        await self.env["main_menu_callback"](callback, state)
        self.assertEqual(state.active, "ReportStates:waiting_for_proof")
        self.assertTrue(callback.replies[-1][1].get("show_alert"))

    async def test_admin_guard_still_applies(self):
        callback, state = Callback(), State()
        callback.from_user.id = 999
        await self.env["main_menu_callback"](callback, state)
        self.assertTrue(callback.replies[-1][1].get("show_alert"))


class TestStaticOrder(unittest.TestCase):
    def test_automatic_ai_stays_last_handler(self):
        self.assertLess(
            BOT.index('@dp.message(ReportStates.waiting_for_proof)'),
            BOT.index("@dp.message(F.text)\nasync def conversational_admin_command")
        )
        self.assertLess(
            BOT.index("@dp.message(GrantStates.waiting_for_details)"),
            BOT.index("@dp.message(F.text)\nasync def conversational_admin_command")
        )
        self.assertLess(
            BOT.index("@dp.message(RegistrationStates.waiting_for_nick)"),
            BOT.index("@dp.message(F.text)\nasync def conversational_admin_command")
        )


if __name__ == "__main__":
    unittest.main()
