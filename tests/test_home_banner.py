"""Owner-only Telegram banner upload and safe main-menu media fallback."""
import ast
import copy
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOT = (ROOT / "bot.py").read_text(encoding="utf-8")
DESIGN = (ROOT / "telegram_design.py").read_text(encoding="utf-8")


class Markup:
    def __init__(self, inline_keyboard):
        self.inline_keyboard = inline_keyboard


class FakeMessage:
    def __init__(self, user_id=42, photo=None, text="/setbanner"):
        self.from_user = types.SimpleNamespace(id=user_id)
        self.chat = types.SimpleNamespace(id=user_id, type="private")
        self.text = text
        self.photo = photo
        self.answers = []
        self.photos = []
        self.reject_photo = False

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))
        return "message-sent"

    async def answer_photo(self, **kwargs):
        self.photos.append(kwargs)
        if self.reject_photo:
            raise RuntimeError("telegram rejected the photo")
        return "photo-sent"


class FakeState:
    def __init__(self, active=None):
        self.active = active
        self.cleared = False

    async def get_state(self):
        return self.active

    async def set_state(self, value):
        self.active = value

    async def clear(self):
        self.active = None
        self.cleared = True


def isolate(source, names, env):
    nodes = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name in names:
            fn = copy.deepcopy(node)
            fn.decorator_list = []
            nodes.append(fn)
    env.setdefault("types", types.SimpleNamespace(Message=FakeMessage))
    env.setdefault("FSMContext", FakeState)
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
                 "<test-menu-banner>", "exec"), env)
    return env


class TestMediaHomeCard(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fallbacks = []

        async def fallback(message, text, **kwargs):
            self.fallbacks.append((message, text, kwargs))
            return "text-fallback"

        def add_lea(markup):
            return Markup(markup.inline_keyboard + [["lea"]])

        self.env = isolate(DESIGN, {"send_home_banner_card"}, {
            "send_fbi_card": fallback,
            "card_html": lambda text: "<b>Личный кабинет</b> " + text,
            "InlineKeyboardMarkup": Markup,
            "add_lea_button": add_lea,
        })

    async def test_photo_caption_contains_menu_and_buttons(self):
        message = FakeMessage()
        buttons = Markup([["report"], ["status"]])
        result = await self.env["send_home_banner_card"](
            message, "агентский кабинет", reply_markup=buttons,
            banner_file_id="valid-telegram-photo-file-id", lea=True
        )
        self.assertEqual(result, "photo-sent")
        self.assertEqual(message.photos[0]["photo"], "valid-telegram-photo-file-id")
        self.assertIn("агентский кабинет", message.photos[0]["caption"])
        self.assertEqual(message.photos[0]["reply_markup"].inline_keyboard,
                         [["report"], ["status"], ["lea"]])
        self.assertEqual(self.fallbacks, [])

    async def test_empty_banner_uses_original_text_card(self):
        message = FakeMessage()
        buttons = Markup([["report"]])
        result = await self.env["send_home_banner_card"](
            message, "кабинет", reply_markup=buttons, banner_file_id="", lea=True
        )
        self.assertEqual(result, "text-fallback")
        self.assertEqual(self.fallbacks[0][2]["reply_markup"], buttons)
        self.assertTrue(self.fallbacks[0][2]["lea"])
        self.assertEqual(message.photos, [])

    async def test_telegram_failure_retains_original_keyboard(self):
        message = FakeMessage()
        message.reject_photo = True
        buttons = Markup([["report"], ["status"]])
        result = await self.env["send_home_banner_card"](
            message, "кабинет", reply_markup=buttons,
            banner_file_id="expired-file-id", lea=True
        )
        self.assertEqual(result, "text-fallback")
        self.assertIs(self.fallbacks[0][2]["reply_markup"], buttons)
        self.assertTrue(self.fallbacks[0][2]["lea"])


class TestBannerManagement(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.saved = []
        self.home_sent = []
        self.allow_saving = True
        self.owner_id = 42

        async def save():
            self.saved.append(self.env["MENU_BANNER_FILE_ID"])
            return self.allow_saving

        async def show_home(message, text, **kwargs):
            self.home_sent.append((text, kwargs))

        self.env = isolate(
            BOT, {"cmd_set_menu_banner", "cmd_remove_menu_banner",
                  "receive_menu_banner_photo"},
            {
                "is_owner": lambda uid: uid == self.owner_id,
                "MenuBannerStates": types.SimpleNamespace(
                    waiting_for_photo="banner-photo"
                ),
                "MENU_BANNER_FILE_ID": "",
                "sync_state_to_cloud": save,
                "users_db": {"42": {"nick": "Gomez_Addams"}},
                "home_card_text": lambda profile, uid: profile["nick"],
                "get_main_menu": lambda uid: "inline-buttons",
                "send_home_banner_card": show_home,
            }
        )

    async def test_only_owner_can_begin_or_remove_banner(self):
        message, state = FakeMessage(user_id=77), FakeState()
        await self.env["cmd_set_menu_banner"](message, state)
        self.assertTrue(message.answers)
        self.assertIsNone(state.active)
        await self.env["cmd_remove_menu_banner"](message, state)
        self.assertEqual(self.saved, [])

    async def test_active_report_cannot_be_interrupted(self):
        message, state = FakeMessage(), FakeState("report:waiting_for_proof")
        await self.env["cmd_set_menu_banner"](message, state)
        self.assertEqual(state.active, "report:waiting_for_proof")
        self.assertEqual(self.saved, [])

    async def test_owner_photo_is_persisted_then_shown(self):
        message, state = FakeMessage(photo=[
            types.SimpleNamespace(
                width=1280, height=720, file_id="valid-telegram-photo-file-id")
        ]), FakeState("banner-photo")
        await self.env["receive_menu_banner_photo"](message, state)
        self.assertTrue(state.cleared)
        self.assertEqual(self.env["MENU_BANNER_FILE_ID"], "valid-telegram-photo-file-id")
        self.assertEqual(self.saved, ["valid-telegram-photo-file-id"])
        self.assertEqual(len(self.home_sent), 1)
        self.assertEqual(
            self.home_sent[0][1]["banner_file_id"], "valid-telegram-photo-file-id"
        )
        self.assertEqual(self.home_sent[0][1]["reply_markup"], "inline-buttons")

    async def test_firebase_failure_restores_original_and_keeps_state(self):
        self.env["MENU_BANNER_FILE_ID"] = "old-banner-telegram-file-id"
        self.allow_saving = False
        message, state = FakeMessage(photo=[
            types.SimpleNamespace(
                width=1280, height=720, file_id="new-telegram-photo-file-id")
        ]), FakeState("banner-photo")
        await self.env["receive_menu_banner_photo"](message, state)
        self.assertFalse(state.cleared)
        self.assertEqual(state.active, "banner-photo")
        self.assertEqual(self.env["MENU_BANNER_FILE_ID"], "old-banner-telegram-file-id")
        self.assertEqual(self.home_sent, [])

    async def test_wrong_aspect_ratio_is_rejected_before_firebase(self):
        message, state = FakeMessage(photo=[
            types.SimpleNamespace(width=1000, height=1000, file_id="file-id-example")
        ]), FakeState("banner-photo")
        await self.env["receive_menu_banner_photo"](message, state)
        self.assertEqual(self.saved, [])
        self.assertFalse(state.cleared)

    async def test_remove_banner_without_touching_users(self):
        self.env["MENU_BANNER_FILE_ID"] = "previous-valid-telegram-file-id"
        message, state = FakeMessage(), FakeState()
        await self.env["cmd_remove_menu_banner"](message, state)
        self.assertEqual(self.env["MENU_BANNER_FILE_ID"], "")
        self.assertEqual(self.saved, [""])
        self.assertEqual(self.env["users_db"]["42"]["nick"], "Gomez_Addams")


class TestHomeRouteStatic(unittest.TestCase):
    def test_all_home_routes_use_banner_helper(self):
        self.assertEqual(BOT.count("await send_home_banner_card("), 5)
        self.assertIn('menu_banner_file_id": MENU_BANNER_FILE_ID', BOT)
        self.assertIn('cloud_data.get("menu_banner_file_id", "")', BOT)

    def test_access_guard_and_acl_integrity(self):
        self.assertIn("not is_owner(uid)", BOT)
        self.assertIn("sign_admin_permissions(admins_db, admin_roles)", BOT)
        self.assertNotIn("BOT_TOKEN", DESIGN)


if __name__ == "__main__":
    unittest.main()
