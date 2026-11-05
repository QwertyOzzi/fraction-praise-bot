"""Native Telegram command menu and fresh navigation regression checks."""
import ast
import asyncio
import copy
import html
import types
import unittest
from pathlib import Path
from typing import List

ROOT = Path(__file__).resolve().parents[1]
BOT = (ROOT / "bot.py").read_text("utf-8")
DESIGN = (ROOT / "telegram_design.py").read_text("utf-8")
TREE = ast.parse(BOT)


class Data:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class BotStub:
    def __init__(self):
        self.sets, self.deletes, self.menu_buttons = [], [], []

    async def set_my_commands(self, commands, scope):
        self.sets.append((commands, scope))

    async def delete_my_commands(self, scope):
        self.deletes.append(scope)

    async def set_chat_menu_button(self, menu_button):
        self.menu_buttons.append(menu_button)


class Logger:
    def warning(self, *args):
        pass


def function_env(names, env, *, with_catalog=False):
    nodes = []
    for node in TREE.body:
        if with_catalog and isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name)
            and target.id in ("PUBLIC_BOT_COMMANDS", "ADMIN_BOT_COMMANDS", "OWNER_BOT_COMMANDS")
            for target in node.targets
        ):
            nodes.append(copy.deepcopy(node))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            fn = copy.deepcopy(node)
            fn.decorator_list = []
            nodes.append(fn)
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
                 "<isolated menu handlers>", "exec"), env)
    return env


class TestNativeCommandCatalog(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = BotStub()
        self.env = function_env({
            "commands_for_user", "sync_user_command_scope", "register_native_command_menu"
        }, {
            "List": List, "BotCommand": Data, "BotCommandScopeDefault": Data,
            "BotCommandScopeChat": Data, "MenuButtonCommands": Data,
            "bot": self.bot, "logger": Logger(),
            "OWNER_TELEGRAM_IDS": {1}, "admins_db": [2], "_cloud_state_hydrated": True,
            "is_admin": lambda uid: uid in (1, 2), "is_owner": lambda uid: uid == 1,
            "is_leader": lambda uid: uid == 1
        }, with_catalog=True)

    def test_user_admin_owner_have_relevant_commands(self):
        user = [c.command for c in self.env["commands_for_user"](3)]
        admin = [c.command for c in self.env["commands_for_user"](2)]
        owner = [c.command for c in self.env["commands_for_user"](1)]
        self.assertIn("menu", user)
        self.assertIn("help", user)
        self.assertIn("report", user)
        self.assertIn("rank", user)
        self.assertIn("min", user)
        self.assertIn("plus", user)
        self.assertIn("status", user)
        self.assertNotIn("agents", user)
        self.assertIn("agents", admin)
        self.assertIn("pending", admin)
        self.assertIn("grant", admin)
        self.assertIn("agentpending", admin)
        self.assertIn("agentlogs", admin)
        self.assertNotIn("news", admin)
        self.assertIn("news", owner)
        self.assertNotIn("security", admin)
        self.assertIn("security", owner)
        self.assertIn("notifications", owner)
        self.assertIn("setbanner", owner)
        self.assertIn("removebanner", owner)
        self.assertNotIn("setbanner", admin)
        self.assertNotIn("setbanner", user)

    async def test_startup_registers_global_menu_and_role_scopes(self):
        await self.env["register_native_command_menu"]()
        self.assertEqual(len(self.bot.menu_buttons), 1)
        self.assertEqual(len(self.bot.sets), 3)
        chat_scopes = [scope.chat_id for _, scope in self.bot.sets
                       if hasattr(scope, "chat_id")]
        self.assertEqual(chat_scopes, [1, 2])
        self.assertFalse(self.bot.deletes)

    async def test_revoke_reverts_to_global_commands(self):
        self.env["is_admin"] = lambda uid: False
        await self.env["sync_user_command_scope"](2)
        self.assertEqual([scope.chat_id for scope in self.bot.deletes], [2])


class State:
    def __init__(self):
        self.cleared = False

    async def clear(self):
        self.cleared = True


class Message:
    def __init__(self, private=True):
        self.chat = Data(type="private" if private else "group")
        self.from_user = Data(id=10)
        self.text = "/menu"
        self.replies = []

    async def answer(self, text, **kwargs):
        self.replies.append((text, kwargs))


class TestFreshMenu(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cards = []
        self.changed = []

        async def send_card(message, text, **kwargs):
            self.cards.append((message, text, kwargs))

        async def sync(uid):
            self.changed.append(uid)

        self.env = function_env({"cmd_menu"}, {
            "types": Data(Message=Message), "FSMContext": State,
            "users_db": {"10": {"nick": "Gomez_Addams", "rank": "8"}},
            "send_fbi_card": send_card,
            "send_home_banner_card": send_card,
            "MENU_BANNER_FILE_ID": "stored-photo-id",
            "sync_user_command_scope": sync,
            "home_card_text": lambda profile, uid: profile["nick"],
            "get_main_menu": lambda uid: "inline-keyboard",
        })

    async def test_menu_sends_new_card_near_bottom(self):
        msg, state = Message(), State()
        await self.env["cmd_menu"](msg, state)
        self.assertTrue(state.cleared)
        self.assertEqual(len(self.cards), 1)
        self.assertEqual(self.cards[0][1], "Gomez_Addams")
        self.assertEqual(self.cards[0][2]["reply_markup"], "inline-keyboard")
        self.assertTrue(self.cards[0][2]["lea"])
        self.assertEqual(self.cards[0][2]["banner_file_id"], "stored-photo-id")
        self.assertEqual(self.changed, [10])

    async def test_group_does_not_show_profile(self):
        msg, state = Message(private=False), State()
        await self.env["cmd_menu"](msg, state)
        self.assertEqual(len(self.cards), 0)
        self.assertEqual(len(msg.replies), 1)


class TestNavigationStatic(unittest.TestCase):
    def test_menu_handlers_precede_fsm_consumers(self):
        self.assertLess(BOT.index('@dp.message(Command("menu", "help"))'),
                        BOT.index('@dp.message(RegistrationStates.waiting_for_nick)'))
        self.assertLess(BOT.index('@dp.message(Command("report", "status", "nick", "rank", "support", "ai", "profile", "stats", "department", "permissions"))'),
                        BOT.index('@dp.message(ReportStates.waiting_for_proof)'))
        self.assertIn('await register_native_command_menu()', BOT)

    def test_role_sync_after_confirmed_changes(self):
        for handler in ("callback_recover_admins", "callback_admin_grant",
                        "process_remove_admin"):
            fn = next(n for n in TREE.body if isinstance(n, ast.AsyncFunctionDef)
                      and n.name == handler)
            body = ast.get_source_segment(BOT, fn)
            self.assertIn("await sync_user_command_scope(", body)
            self.assertIn("await sync_state_to_cloud()", body)

    def test_contextual_home_keeps_other_buttons(self):
        tree = ast.parse(DESIGN)
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                  and n.name == "with_home_button")
        env = {"InlineKeyboardButton": Data, "InlineKeyboardMarkup": Data}
        exec(compile(ast.Module(body=[fn], type_ignores=[]),
                     "isolated nav", "exec"), env)
        initial = Data(inline_keyboard=[[Data(text="Управление", callback_data="agpage:0")]])
        updated = env["with_home_button"](initial)
        self.assertEqual(initial.inline_keyboard[0][0].callback_data, "agpage:0")
        self.assertEqual(updated.inline_keyboard[-1][0].callback_data, "main:open")
        self.assertEqual(len(env["with_home_button"](updated).inline_keyboard),
                         len(updated.inline_keyboard))


if __name__ == "__main__":
    unittest.main()
