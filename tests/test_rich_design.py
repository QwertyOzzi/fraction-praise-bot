"""Regression checks for stable HTML cards and compact, permission-scoped menus."""
import ast
import html
import re
import unittest
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[1]
DESIGN = (ROOT / "telegram_design.py").read_text("utf-8")
BOT = (ROOT / "bot.py").read_text("utf-8")


class Button:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class Markup:
    def __init__(self, inline_keyboard):
        self.inline_keyboard = inline_keyboard


def load_functions(source, names, env):
    tree = ast.parse(source)
    nodes = [node for node in tree.body
             if isinstance(node, ast.FunctionDef) and node.name in names]
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
                 "<isolated GUI functions>", "exec"), env)
    return env


class TestHtmlCards(unittest.TestCase):
    def setUp(self):
        self.env = load_functions(DESIGN, {"card_html", "add_lea_button"}, {
            "TITLE_RE": re.compile(r"^([^\n<]*)<b>([^<\n]+)</b>\n+"),
            "InlineKeyboardMarkup": Markup,
            "InlineKeyboardButton": Button,
            "LEA_URL": "https://lea-script.space/",
            "LEA_LABEL": "🧩 ЛЕА ↗",
        })

    def test_header_and_native_quote(self):
        result = self.env["card_html"](
            "🛡️ <b>FBI · Личный кабинет</b>\n\n"
            "👤 <b>Агент:</b> <code>QA_TestAgent2</code>\n"
            "🎖️ <b>Ранг:</b> 8\n\n<i>Выберите раздел.</i>"
        )
        self.assertIn("<b>FBI · Личный кабинет</b>", result)
        self.assertIn("<blockquote>👤", result)
        self.assertIn("</blockquote>", result)
        self.assertIn("<code>QA_TestAgent2</code>", result)
        self.assertNotIn("━", result)

    def test_existing_buttons_preserved_when_adding_lea(self):
        old = Markup([[Button(text="📝 Отчёт", callback_data="main:report")]])
        new = self.env["add_lea_button"](old)
        self.assertEqual(len(old.inline_keyboard), 1)
        self.assertEqual(new.inline_keyboard[0][0].callback_data, "main:report")
        self.assertEqual(new.inline_keyboard[-1][0].url, "https://lea-script.space/")
        self.assertEqual(new.inline_keyboard[-1][0].text, "🧩 ЛЕА ↗")

    def test_no_experimental_rich_message_calls(self):
        self.assertNotIn("answer_rich", DESIGN)
        self.assertNotIn("InputRichMessage", DESIGN)
        self.assertIn('parse_mode="HTML"', DESIGN)
        self.assertIn("ReplyKeyboardRemove()", DESIGN)


class TestInlineMenu(unittest.TestCase):
    def setUp(self):
        self.env = load_functions(BOT, {"get_main_menu", "home_card_text"}, {
            "Dict": Dict, "Any": Any, "html": html,
            "InlineKeyboardButton": Button, "InlineKeyboardMarkup": Markup,
            "WebAppInfo": Button, "WEBAPP_URL": "https://example.org",
            "is_admin": lambda uid: uid == 10,
            "role_name": lambda uid: "Владелец" if uid == 10 else "Агент",
        })

    def test_agent_has_no_admin_controls(self):
        menu = self.env["get_main_menu"](20)
        codes = [button.callback_data for row in menu.inline_keyboard
                 for button in row if getattr(button, "callback_data", None)]
        self.assertEqual(codes, ["main:report", "main:status", "main:nick", "main:support"])
        self.assertFalse(any("admin" in code for code in codes))

    def test_admin_gets_separate_panel(self):
        menu = self.env["get_main_menu"](10)
        codes = [getattr(button, "callback_data", None)
                 for row in menu.inline_keyboard for button in row]
        self.assertIn("main:admin", codes)
        panel = self.env["get_main_menu"](10, admin_panel=True)
        codes = [button.callback_data for row in panel.inline_keyboard for button in row]
        self.assertIn("main:agents", codes)
        self.assertIn("main:grant", codes)
        self.assertIn("main:home", codes)

    def test_home_escapes_profile(self):
        result = self.env["home_card_text"]({"nick": "<Fake_Nick>", "rank": "8"}, 20)
        self.assertIn("&lt;Fake_Nick&gt;", result)
        self.assertNotIn("<Fake_Nick>", result)

    def test_callback_uses_real_sender_and_has_admin_guard(self):
        fn = next(n for n in ast.parse(BOT).body
                  if isinstance(n, ast.AsyncFunctionDef)
                  and n.name == "main_menu_callback")
        body = ast.get_source_segment(BOT, fn)
        self.assertIn("callback.from_user.id", body)
        self.assertIn("callback.message.chat.id != callback.from_user.id", body)
        self.assertIn("not is_admin(uid)", body)
        self.assertIn('update={"from_user": callback.from_user}', body)
        self.assertIn("await state.clear()", body)


if __name__ == "__main__":
    unittest.main()
