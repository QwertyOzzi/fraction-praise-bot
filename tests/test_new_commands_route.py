"""New personnel commands must route despite unfinished registration/report FSM."""
import ast
import copy
import types
import unittest
from pathlib import Path

SOURCE = (Path(__file__).resolve().parents[1] / "bot.py").read_text("utf-8")
TREE = ast.parse(SOURCE)


class State:
    def __init__(self):
        self.active = "ReportStates:waiting_for_proof"
        self.cleared = False

    async def get_state(self):
        return self.active

    async def clear(self):
        self.active = None
        self.cleared = True


class Message:
    def __init__(self, text, private=True):
        self.text = text
        self.chat = types.SimpleNamespace(type="private" if private else "group")
        self.answers = []

    async def answer(self, value):
        self.answers.append(value)


class TestNewCommandRouting(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.calls = []
        self.env = {"types": types.SimpleNamespace(Message=Message),
                    "FSMContext": State}
        for name in (
            "cmd_employee_profile", "cmd_activity_stats",
            "cmd_set_department", "cmd_permissions"
        ):
            async def capture(message, _name=name):
                self.calls.append((_name, message.text))
            self.env[name] = capture
        node = next(node for node in TREE.body
                    if isinstance(node, ast.AsyncFunctionDef)
                    and node.name == "cmd_quick_navigation")
        node = copy.deepcopy(node)
        node.decorator_list = []
        exec(compile(ast.fix_missing_locations(
            ast.Module(body=[node], type_ignores=[])), "<command routing>", "exec"),
            self.env)

    async def test_new_commands_clear_stale_state_and_dispatch(self):
        for command, function in (
            ("/profile", "cmd_employee_profile"),
            ("/stats", "cmd_activity_stats"),
            ("/department Test_Agent HRT", "cmd_set_department"),
            ("/permissions", "cmd_permissions")
        ):
            state, message = State(), Message(command)
            await self.env["cmd_quick_navigation"](message, state)
            self.assertTrue(state.cleared)
            self.assertEqual(self.calls[-1], (function, command))

    async def test_private_only(self):
        state = State()
        await self.env["cmd_quick_navigation"](Message("/stats", private=False), state)
        self.assertFalse(state.cleared)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
