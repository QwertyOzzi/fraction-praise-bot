"""Runtime-style regression tests without starting Telegram or connecting to Firebase."""
import ast
import asyncio
import copy
import html
import types as pytypes
import unittest
from datetime import datetime
from pathlib import Path

SOURCE = (Path(__file__).resolve().parents[1] / "bot.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


def isolated_handler(name, env):
    node = next(n for n in TREE.body
                if isinstance(n, ast.AsyncFunctionDef) and n.name == name)
    isolated = copy.deepcopy(node)
    isolated.decorator_list = []
    module = ast.fix_missing_locations(ast.Module(body=[isolated], type_ignores=[]))
    exec(compile(module, "<isolated handler>", "exec"), env)
    return env[name]


class FakeState:
    def __init__(self, nick="New_Nick"):
        self.data = {"nick": nick}
        self.cleared = False

    async def get_data(self):
        return self.data

    async def clear(self):
        self.cleared = True


class FakeMessage:
    def __init__(self):
        self.sent = []
        self.edited = []

    async def edit_text(self, *args, **kwargs):
        self.edited.append((args, kwargs))

    async def answer(self, *args, **kwargs):
        self.sent.append((args, kwargs))


class FakeCallback:
    def __init__(self):
        self.data = "rank_8"
        self.from_user = pytypes.SimpleNamespace(id=12345, username="someusername")
        self.message = FakeMessage()
        self.answered = []

    async def answer(self, *args, **kwargs):
        self.answered.append((args, kwargs))


class TestGlobalCancellation(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_is_registered_before_all_state_input_handlers(self):
        cancel_idx = SOURCE.index('@dp.message(Command("cancel"))')
        for decorator in (
            "@dp.message(RegistrationStates.waiting_for_nick)",
            "@dp.message(ReportStates.waiting_for_quantity)",
            "@dp.message(ReportStates.waiting_for_proof)",
            "@dp.message(GrantStates.waiting_for_details)",
            "@dp.message(SupportStates.waiting_for_message)",
        ):
            self.assertLess(cancel_idx, SOURCE.index(decorator), decorator)
        self.assertEqual(SOURCE.count('@dp.message(Command("cancel"))'), 1)

    async def test_cancel_clears_active_state_and_returns_menu(self):
        env = {
            "types": pytypes.SimpleNamespace(Message=object),
            "FSMContext": object,
            "get_main_menu": lambda uid: "TEST MENU",
        }
        fn = isolated_handler("cmd_cancel", env)
        state = FakeState()
        message = FakeMessage()
        message.from_user = pytypes.SimpleNamespace(id=12345)
        await fn(message, state)
        self.assertTrue(state.cleared)
        self.assertEqual(message.sent[-1][1]["reply_markup"], "TEST MENU")

    async def test_proof_handler_handles_cancel_as_fallback(self):
        self.assertIn('await cmd_cancel(message, state)',
                      SOURCE.split('async def process_report_proof(', 1)[1]
                            .split('@dp.callback_query(F.data == "cancel_report")', 1)[0])
        self.assertIn('await cmd_cancel(message, state)',
                      SOURCE.split('async def process_report_quantity_text(', 1)[1]
                            .split('async def process_report_reason(', 1)[0])


class TestDurableProfile(unittest.IsolatedAsyncioTestCase):
    def make_context(self, persist_success):
        user_data = {"12345": {"nick": "Old_Nick", "rank": "7", "registered_at": "2026-01-01"}}
        writes = []
        audit_events = []

        async def persist(*args):
            writes.append("cloud")
            if not persist_success:
                return False, "Firebase error", "", None
            return True, "Saved", "Old_Nick", {
                "nick": "New_Nick", "rank": "8", "telegram_username": "someusername",
                "registered_at": "2026-01-01", "profileId": "a" * 32
            }

        async def audit(*args):
            audit_events.append(args)

        env = {
            "types": pytypes.SimpleNamespace(CallbackQuery=object),
            "FSMContext": object,
            "datetime": datetime, "html": html, "asyncio": asyncio,
            "users_db": user_data, "USERS_FILE": "test-users.json",
            "_cloud_state_hydrated": True, "_state_sync_lock": asyncio.Lock(),
            "firebase_client": object(), "FIREBASE_PROJECT_ID": "test",
            "sign_admin_permissions": lambda *a: "valid",
            "register_profile": persist,
            "save_json": lambda *args: writes.append("local"),
            "record_profile_activity": audit,
            "get_main_menu": lambda uid: "TEST MENU",
        }
        fn = isolated_handler("process_rank", env)
        return fn, user_data, writes, audit_events

    async def test_success_requires_cloud_confirmation_then_local_save(self):
        fn, db, writes, events = self.make_context(True)
        cb, state = FakeCallback(), FakeState()
        await fn(cb, state)
        await asyncio.sleep(0)
        self.assertEqual(db["12345"]["nick"], "New_Nick")
        self.assertEqual(db["12345"]["registered_at"], "2026-01-01")
        self.assertEqual(writes, ["cloud", "local"])
        self.assertTrue(state.cleared)
        self.assertIn("Профиль и таблицы синхронизированы", cb.message.edited[0][0][0])
        self.assertEqual(events[0][1], "profile_changed")

    async def test_failure_rolls_back_and_allows_retry(self):
        fn, db, writes, events = self.make_context(False)
        cb, state = FakeCallback(), FakeState()
        await fn(cb, state)
        self.assertEqual(db["12345"]["nick"], "Old_Nick")
        self.assertEqual(writes, ["cloud"])
        self.assertFalse(state.cleared)
        self.assertFalse(cb.message.edited)
        self.assertEqual(events, [])
        self.assertIn("Firebase", cb.answered[-1][0][0])


if __name__ == "__main__":
    unittest.main()
