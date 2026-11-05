"""Ensure /help lists every actual bot command without opening the main menu."""
import ast
import copy
import html
import unittest
from pathlib import Path

from help_catalog import (
    AGENT_COMMANDS, DEPUTY_COMMANDS, LEADER_COMMANDS, OWNER_COMMANDS,
    all_help_commands, full_help_text,
)

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "bot.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


def declared_bot_commands():
    """Walk handler decorators, not just per-role Telegram slash menus."""
    names = {"start"}  # @dp.message(CommandStart())
    for node in TREE.body:
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        for decorator in node.decorator_list:
            for part in ast.walk(decorator):
                if (isinstance(part, ast.Call) and isinstance(part.func, ast.Name)
                        and part.func.id == "Command"):
                    names.update(
                        arg.value for arg in part.args
                        if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                    )
    return names


class TestHelpCatalog(unittest.TestCase):
    def test_contains_every_registered_command(self):
        registered = declared_bot_commands()
        listed = set(all_help_commands())
        self.assertEqual(registered - listed, set(),
                         "A registered command is missing from /help")
        self.assertEqual(listed - registered, set(),
                         "The reference advertises a nonexistent command")

    def test_no_duplicates_and_user_can_see_all_role_sections(self):
        commands = all_help_commands()
        self.assertEqual(len(commands), len(set(commands)))
        view = full_help_text()
        for cmd in commands:
            self.assertIn(f"<code>/{cmd}</code>", view)
        for heading in ("АГЕНТЫ", "РУКОВОДСТВО", "ЛИДЕР", "ВЛАДЕЛЕЦ"):
            self.assertIn(heading, view)
        self.assertIn("не предоставляет доступ", view)
        self.assertLess(len(html.unescape(view)), 4096)

    def test_clear_role_boundaries(self):
        self.assertIn("help", dict(AGENT_COMMANDS))
        self.assertIn("grant", dict(DEPUTY_COMMANDS))
        self.assertIn("news", dict(LEADER_COMMANDS))
        self.assertIn("permissions", dict(OWNER_COMMANDS))


class _Message:
    def __init__(self, private=True):
        self.chat = type("Chat", (), {"type": "private" if private else "group"})()
        self.responses = []

    async def answer(self, text, **kwargs):
        self.responses.append((text, kwargs))


class TestHelpRoute(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        node = next(n for n in TREE.body if isinstance(n, ast.AsyncFunctionDef)
                    and n.name == "cmd_help")
        node = copy.deepcopy(node)
        node.decorator_list = []
        env = {"types": type("Types", (), {"Message": _Message}),
               "full_help_text": full_help_text}
        exec(compile(ast.fix_missing_locations(
            ast.Module(body=[node], type_ignores=[])), "<help>", "exec"), env)
        self.handler = env["cmd_help"]

    async def test_private_help_returns_reference_not_menu(self):
        message = _Message()
        await self.handler(message)
        self.assertEqual(len(message.responses), 1)
        text, kwargs = message.responses[0]
        self.assertIn("<code>/report</code>", text)
        self.assertIn("<code>/security</code>", text)
        self.assertNotIn("reply_markup", kwargs)
        self.assertEqual(kwargs["parse_mode"], "HTML")

    async def test_group_shows_no_privileged_actions(self):
        message = _Message(private=False)
        await self.handler(message)
        self.assertEqual(len(message.responses), 1)
        self.assertNotIn("/permissions", message.responses[0][0])

    def test_help_handler_precedes_fsm_and_does_not_clear_it(self):
        help_at = SOURCE.index('@dp.message(Command("help"))')
        assert_at = SOURCE.index('@dp.message(ReportStates.waiting_for_proof)')
        self.assertLess(help_at, assert_at)
        handler = next(n for n in TREE.body if isinstance(n, ast.AsyncFunctionDef)
                       and n.name == "cmd_help")
        code = ast.get_source_segment(SOURCE, handler)
        self.assertNotIn("state.clear()", code)
        self.assertNotIn("get_main_menu(", code)
        self.assertNotIn("send_home_banner_card(", code)


if __name__ == "__main__":
    unittest.main()
